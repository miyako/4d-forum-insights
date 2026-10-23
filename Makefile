# 4D Forum Insights: build the static site's data from the fminer database.
#
#   make all          snapshot + data + embeddings (run once ~/4d-feature-miner has finished)
#   make serve        preview on http://127.0.0.1:8000
#   make publish      upload site/data as a release asset and redeploy GitHub Pages
#
# The source DB is only ever read: `snapshot` copies it with sqlite's online .backup (read-only open).

FMINER_DB_SRC ?= $(HOME)/4d-feature-miner/data/forum.db
DB            ?= build/forum.db
WORK_DB       ?= build/work.db
RELEASE       ?= data
PY            := uv run -q --project pipeline python
PORT          ?= 8000

.PHONY: all snapshot data embeddings embeddings-sample vendor test serve site-data publish \
        db-pull db-push db-seed delta-local clean

all: snapshot data embeddings

## copy the live pipeline DB (read-only) into build/forum.db
snapshot:
	@mkdir -p build
	sqlite3 -readonly "$(FMINER_DB_SRC)" ".backup '$(DB).tmp'"
	sqlite3 "$(DB).tmp" "PRAGMA journal_mode=DELETE;" >/dev/null
	mv "$(DB).tmp" "$(DB)"
	@sqlite3 -readonly "$(DB)" "SELECT 'topics', COUNT(*), 'enriched', COUNT(enriched_at) FROM topics; SELECT 'classifications', COUNT(*) FROM classifications;"

## export the published data (manifest, topics, text, answers) into site/data
data:
	$(PY) scripts/build_data.py --db "$(DB)"

## embed every new or changed topic (incremental; ~20 min for 17k topics on a laptop CPU)
embeddings: node_modules
	node scripts/build_embeddings.mjs

## development: embed a deterministic sample of 400 topics
embeddings-sample: node_modules
	node scripts/build_embeddings.mjs --sample 400

node_modules: package.json
	npm install --no-audit --no-fund
	@touch node_modules

## refresh site/vendor from node_modules
vendor: node_modules
	node scripts/vendor.mjs

test: node_modules
	npm test

serve:
	@echo "http://127.0.0.1:$(PORT)/"
	python3 -m http.server $(PORT) --bind 127.0.0.1 -d site

## site/data -> build/site-data.tar.gz
site-data:
	@test -f site/data/manifest.json || { echo "site/data is empty: run make data first"; exit 1; }
	tar -czf build/site-data.tar.gz -C site data
	@du -h build/site-data.tar.gz

## upload site-data.tar.gz to the "data" release and redeploy Pages
publish: site-data
	gh release view $(RELEASE) >/dev/null 2>&1 || gh release create $(RELEASE) --title "Published data" --latest=false \
	  --notes "Data assets for the site (site-data.tar.gz) and the encrypted pipeline DB. Updated by the delta workflow."
	gh release upload $(RELEASE) build/site-data.tar.gz --clobber
	gh workflow run pages.yml

## encrypted working DB of the delta workflow (needs DB_KEY)
db-pull:
	scripts/db-store.sh pull "$(WORK_DB)"
db-push:
	scripts/db-store.sh push "$(WORK_DB)"
## seed the delta workflow's DB from a snapshot of the local pipeline (after it has finished)
db-seed: snapshot
	scripts/db-store.sh push "$(DB)"

## run the delta (fetch + enrich + classify) locally against the working DB, e.g. on a Mac with Metal
delta-local:
	cd pipeline && FMINER_DB="$(abspath $(WORK_DB))" LLAMA_SERVER="$${LLAMA_SERVER:-llama-server}" ci/run-delta.sh

clean:
	rm -rf build/site-data.tar.gz build/*.tmp
