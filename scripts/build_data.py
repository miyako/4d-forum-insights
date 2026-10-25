#!/usr/bin/env python3
"""Build the published site data from the fminer SQLite database.

The database is opened READ-ONLY (``mode=ro``); point ``--db`` at a snapshot (``make snapshot``) rather
than at the live database of a running pipeline. Writes into ``--out`` (default ``site/data``):

  manifest.json        what the UI is generated from: questions, runs, categories, tags, presets, files
  topics.json          one row per topic (columnar), no post text
  text.json            the one-paragraph LLM summaries (loaded lazily by the browser)
  answers/<run>.json   per model run: compact model answers for every classified topic
  tips/entries.json    knowledge-base entries (generated question/headline + answer) for the /tips/ page;
                       the "tips-…" runs (topic area, Q&A kind) are listed under manifest.tips, not manifest.runs
  voices/entries.json  Forum Voices (/voices/): the selected discussions with their ratings and anonymous write-ups
                       (manifest.voices); the internal "heated" rating and any write-up naming someone are left out

and ``build/embed-input.jsonl`` (NOT published) for ``scripts/build_embeddings.mjs``.

Only titles, English titles, LLM summaries, metadata and model answers are published — never post text.
Works on partial data (unenriched / unclassified topics are included with what is known).
"""

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline" / "src"))

FLAG_CLOSED, FLAG_ARCHIVED, FLAG_ACCEPTED, FLAG_ENRICHED, FLAG_LANG_GUESSED = 1, 2, 4, 8, 16
LANG_NAMES = {"en": "English", "fr": "French", "de": "German", "ja": "Japanese", "es": "Spanish", "it": "Italian"}
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def connect_ro(path):
    con = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def days(iso):
    """ISO timestamp → days since 1970-01-01 (compact, sortable)."""
    if not iso:
        return None
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return int((dt - EPOCH).total_seconds() // 86400)


def permille(p):
    return None if p is None else int(round(float(p) * 1000))


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def lang_detector():
    """Reuse the pipeline's cheap stopword detector for topics that are not enriched yet."""
    try:
        from fminer.enrich import detect_lang
        return detect_lang
    except Exception:  # pipeline deps missing: fall back to the category hint
        hints = {"french-community": "fr", "german-community": "de", "japanese-community": "ja", "spanish-forum": "es"}
        return lambda text, slug=None: hints.get(slug, "en")


def upstream_questions(module="classify"):
    try:
        if module == "kb":
            from fminer.kb import QUESTIONS
        else:
            from fminer.classify import QUESTIONS
        return QUESTIONS
    except Exception:
        return {}


SIDE_RUNS = ("tips-", "voices-")  # side views with their own pages; never main-page runs


def is_tips_run(version):
    return (version or "").startswith("tips-")


def is_main_run(version):
    return not (version or "").startswith(SIDE_RUNS)


def strip_markup(text, limit):
    text = re.sub(r"```.*?```|\[code\].*?\[/code\]", " ", text or "", flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)|<[^>]+>|\[/?quote[^\]]*\]", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def synth_answers(r):
    """Very old rows may lack the full answers JSON: rebuild what the columns hold."""
    a = {}
    if r["fr_score"] is not None:
        a["fr"] = {"type": "score", "score": r["fr_score"], "probabilities": json.loads(r["fr_probs"] or "{}")}
    if r["kind"]:
        a["kind"] = {"type": "choice", "choice": r["kind"], "probabilities": {"feature_request": r["kind_fr_p"]}}
    for q in ("request", "gap"):
        if r[f"{q}_p"] is not None:
            a[q] = {"type": "noul", "noul": r[f"{q}_p"]}
    return a


# --------------------------------------------------------------------------- questions & runs

def describe_question(qid, sample, cfg, upstream):
    """Merge what the answers say (type, levels, choices) with config display labels."""
    c = cfg.get(qid, {})
    up = upstream.get(qid, {})
    qtype = sample.get("type") or up.get("type") or "noul"
    q = {
        "id": qid,
        "type": qtype,
        "label": c.get("label") or qid.replace("_", " ").capitalize(),
        "short": c.get("short") or c.get("label") or qid,
        "description": c.get("description") or up.get("instructions") or "",
        "order": c.get("order", 100),
        "primary": bool(c.get("primary")),
    }
    if qtype == "score":
        legend = sample.get("legend") or {str(i): v for i, v in enumerate(up.get("criteria") or [])} \
            or {k: k for k in sample.get("probabilities", {})}
        keys = sorted(legend, key=lambda k: float(k))
        q["levels"] = [{"key": k, "value": float(k), "label": (c.get("levels") or {}).get(k) or legend[k].capitalize(),
                        "long": legend[k]} for k in keys]
        if c.get("bands"):
            q["bands"] = [{k: b[k] for k in ("key", "label", "long", "quantile") if k in b} for b in c["bands"]["bands"]]
    elif qtype == "choice":
        crit = up.get("criteria") if isinstance(up.get("criteria"), dict) else {}
        keys = list(dict.fromkeys(list((c.get("choices") or {}).keys()) + list(crit) +
                                  list(sample.get("probabilities", {}))))
        q["choices"] = [{"key": k, "label": (c.get("choices") or {}).get(k) or k.replace("_", " ").capitalize(),
                         "long": crit.get(k, "")} for k in keys]
    return q


def run_id(model, version):
    return slugify(f"{model.split('/')[-1]}-{version}")


def quantile(sorted_vals, q):
    return sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))]


def band_cuts(q, scores, cats, cfg_q, fr_cat):
    """Score thresholds (high → low) for a banded score question in one run.

    The model's expected score bunches up mid-scale, so fixed level cut-offs are not useful. Instead each band's
    lower bound is a quantile of the scores the run gave to a reference set (default: the Feature Request
    category), e.g. "high" = at least the score of the reference's 25th percentile. Runs with too few
    reference topics use the configured fixed fallback cut-offs."""
    cfg = cfg_q.get(q["id"], {}).get("bands") or {}
    ref_cat = fr_cat if cfg.get("reference", "feature_request_category") == "feature_request_category" else cfg["reference"]
    ref = sorted(s for s, c in zip(scores, cats) if s is not None and c == ref_cat)
    bands = [b for b in cfg.get("bands", []) if "quantile" in b]
    if len(ref) >= cfg.get("min_reference", 30):
        return {"cuts": [round(quantile(ref, b["quantile"]), 3) for b in bands], "reference": len(ref), "method": "quantile"}
    return {"cuts": cfg.get("fallback", []), "reference": len(ref), "method": "fallback"}


def build_runs(con, topic_ids, cfg_site, cfg_q, upstream, out, topic_cat=None, fr_cat=27, tips=False):
    hidden = set(cfg_site.get("hidden_runs", []))
    runs, questions = [], {}
    for r in con.execute("SELECT model, prompt_version, COUNT(*) n, MAX(created_at) last FROM classifications "
                         "GROUP BY 1,2 ORDER BY n DESC").fetchall():
        model, version = r["model"], r["prompt_version"]
        wanted = is_tips_run(version) if tips else is_main_run(version)
        if model in hidden or f"{model}/{version}" in hidden or not wanted:
            continue
        rows = con.execute(
            "SELECT * FROM classifications WHERE model=? AND prompt_version=? ORDER BY topic_id",
            (model, version)).fetchall()
        ids, parsed = [], []
        for row in rows:
            if row["topic_id"] not in topic_ids:
                continue
            a = json.loads(row["answers"]) if row["answers"] else synth_answers(row)
            ids.append(row["topic_id"])
            parsed.append(a)
        if not ids:
            continue
        # question metadata from the first answer of each question
        qmeta = {}
        for a in parsed:
            for qid, ans in a.items():
                if qid not in qmeta:
                    qmeta[qid] = describe_question(qid, ans, cfg_q, upstream)
                elif qmeta[qid]["type"] == "choice":  # union of choices seen
                    known = {x["key"] for x in qmeta[qid]["choices"]}
                    for k in ans.get("probabilities", {}):
                        if k not in known:
                            qmeta[qid]["choices"].append({"key": k, "label": k.replace("_", " ").capitalize(), "long": ""})
                            known.add(k)
        cols = {}
        for qid, q in qmeta.items():
            col = []
            for a in parsed:
                ans = a.get(qid)
                if ans is None:
                    col.append(None)
                elif q["type"] == "score":
                    probs = ans.get("probabilities") or {}
                    col.append([int(round(ans["score"] * 100))] + [permille(probs.get(l["key"], 0)) for l in q["levels"]])
                elif q["type"] == "choice":
                    probs = ans.get("probabilities") or {}
                    keys = [c["key"] for c in q["choices"]]
                    col.append([keys.index(ans["choice"]) if ans.get("choice") in keys else -1]
                               + [permille(probs.get(k, 0)) for k in keys])
                else:
                    col.append(permille(ans.get("noul", ans.get("p"))))
            cols[qid] = col
        rid = run_id(model, version)
        bands = {qid: band_cuts(q, [a.get(qid, {}).get("score") for a in parsed], [(topic_cat or {}).get(i) for i in ids],
                                cfg_q, fr_cat)
                 for qid, q in qmeta.items() if q.get("bands")}
        keys = {qid: [x["key"] for x in q.get("levels") or q.get("choices")]
                for qid, q in qmeta.items() if q["type"] in ("score", "choice")}
        payload = {"run": rid, "ids": ids, "keys": keys, "answers": cols}
        fname = write_json(out, f"{'tips/' if tips else ''}answers/{rid}.json", payload)
        for qid, q in qmeta.items():
            g = questions.setdefault(qid, dict(q))
            if g["type"] != q["type"]:
                print(f"warning: question {qid!r} is {g['type']} in one run and {q['type']} in {rid}", file=sys.stderr)
                continue
            for part in ("choices", "levels"):  # union across runs (the run files carry their own key order)
                if part in q:
                    known = {x["key"] for x in g[part]}
                    g[part] += [x for x in q[part] if x["key"] not in known]
            if "levels" in g:
                g["levels"].sort(key=lambda x: x["value"])
        label = model.split("/")[-1].replace("-GGUF", "")
        if model.endswith(":original"):
            label = label.replace(":original", "") + " (original text)"
        runs.append({
            "id": rid, "model": model, "prompt_version": version, "label": f"{label} · {version}",
            "count": len(ids), "last": r["last"], "questions": sorted(qmeta, key=lambda k: (qmeta[k]["order"], k)),
            "file": fname, "bands": bands,
        })
    preferred = cfg_site.get("preferred_runs", [])
    default = None
    for m in preferred:
        cand = [x for x in runs if x["model"] == m]
        if cand:
            default = max(cand, key=lambda x: (x["last"] or "", x["count"]))["id"]
            break
    if default is None and runs:
        default = runs[0]["id"]
    qs = sorted(questions.values(), key=lambda q: (q["order"], q["id"]))
    if qs and not any(q["primary"] for q in qs):
        score = [q for q in qs if q["type"] == "score"]
        (score or qs)[0]["primary"] = True
    return runs, qs, default


def build_tips(con, topic_ids, cfg, out):
    """Knowledge base for /tips/: the "tips-…" runs (area, qa) plus the generated entries (kb_entries)."""
    runs, questions, default_run = build_runs(con, topic_ids, cfg, cfg.get("questions", {}), upstream_questions("kb"),
                                              out, tips=True)
    if not runs:
        return None
    has_entries = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kb_entries'").fetchone() is not None
    # only entries whose type still matches the topic's latest rating (a delta can re-rate a topic)
    rows = con.execute(
        """WITH x AS (
               SELECT topic_id, json_extract(answers, '$.qa.choice') AS mode,
                      ROW_NUMBER() OVER (PARTITION BY topic_id ORDER BY created_at DESC) AS rn
               FROM classifications WHERE prompt_version LIKE 'tips-%')
           SELECT k.topic_id, k.mode, k.headline, k.body, k.answered, k.problems
           FROM kb_entries k JOIN x ON x.topic_id = k.topic_id AND x.rn = 1 AND x.mode = k.mode
           WHERE k.headline IS NOT NULL ORDER BY k.topic_id""").fetchall() if has_entries else []
    rows = [r for r in rows if r["topic_id"] in topic_ids]
    modes = sorted({r["mode"] for r in rows})
    entries = {
        "ids": [r["topic_id"] for r in rows],
        "modes": modes,
        "mode": [modes.index(r["mode"]) for r in rows],
        "headline": [r["headline"] for r in rows],
        "body": [r["body"] for r in rows],
        # 1 = the writer confirmed the thread answers/teaches something, 0 = it did not, 2 = it did but the text
        # still fails a quality check (length, cut off, not phrased as a question)
        "ok": [0 if r["answered"] == 0 else 2 if (r["problems"] or "[]") != "[]" else 1 for r in rows],
    }
    return {
        "title": cfg.get("title", "4D Forum Tips"),
        "description": cfg.get("description", ""),
        "runs": runs,
        "questions": questions,
        "default_run": default_run,
        "area_min_p": cfg.get("area_min_p", 0.25),
        "presets": cfg.get("presets", []),
        "entries": len(rows),
        "files": {"entries": write_json(out, "tips/entries.json", entries)},
    }


def build_voices(con, topic_ids, cfg, out):
    """Forum Voices for /voices/: selected threads ("voices-…" run) with a publishable write-up (voices_entries)."""
    try:
        from fminer import voices
        from fminer.scrape import split_raw
    except Exception as e:  # pipeline package missing
        print(f"  voices: skipped ({e})")
        return None
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='voices_entries'").fetchone():
        return None
    runs = con.execute(
        """SELECT model, COUNT(*) n, MAX(created_at) last FROM classifications WHERE prompt_version = ?
           GROUP BY 1 ORDER BY n DESC""", (voices.PROMPT_VERSION,)).fetchall()
    if not runs:
        return None
    model = runs[0]["model"]
    rows = con.execute(
        """SELECT c.topic_id, c.answers, t.thread_raw, v.question, v.positions, v.outcome, v.discussion, v.problems,
                  v.prompt_version AS write_version
           FROM classifications c JOIN topics t ON t.id = c.topic_id
           JOIN voices_entries v ON v.topic_id = c.topic_id AND v.question IS NOT NULL
           WHERE c.model = ? AND c.prompt_version = ? ORDER BY c.topic_id""",
        (model, voices.PROMPT_VERSION)).fetchall()
    keys = {q: list(voices.QUESTIONS[q]["criteria"]) for q in ("exchange", "settled", "subject")}
    cols = {k: [] for k in ("ids", "rank", "debate", "exchange", "settled", "settled_p", "subject", "subject_p",
                            "participants", "returning", "days", "question", "positions", "outcome", "ok")}
    withheld = 0
    for r in rows:
        a = json.loads(r["answers"])
        if r["topic_id"] not in topic_ids or not voices.selected(a):
            continue
        problems = json.loads(r["problems"] or "[]")
        if "names" in problems or "letters" in problems:
            withheld += 1
            continue
        st = voices.thread_stats(split_raw(r["thread_raw"] or ""))
        cols["ids"].append(r["topic_id"])
        cols["rank"].append(permille(voices.rank(a) / 4))
        cols["debate"].append(permille(a["debate"]["score"] / 4))
        for q in ("exchange", "settled", "subject"):
            cols[q].append(keys[q].index(a[q]["choice"]))
            if q != "exchange":
                cols[f"{q}_p"].append(permille(a[q]["probabilities"].get(a[q]["choice"], 0)))
        cols["participants"].append(st["participants"])
        cols["returning"].append(st["returning"])
        cols["days"].append(st["days"])
        cols["question"].append(r["question"])
        cols["positions"].append(json.loads(r["positions"] or "[]"))
        cols["outcome"].append(r["outcome"])
        # 1 = clean, 0 = the writer found no real exchange of views (hidden by default), 2 = minor flaw (length...)
        cols["ok"].append(0 if r["discussion"] == 0 else 2 if problems else 1)
    labels = cfg.get("questions", {})
    questions = [{
        "id": q, "label": labels.get(q, {}).get("label", q),
        "choices": [{"key": k, "label": labels.get(q, {}).get("choices", {}).get(k, k),
                     "long": voices.QUESTIONS[q]["criteria"][k]} for k in keys[q]],
    } for q in ("settled", "subject", "exchange")]
    return {
        "title": cfg.get("title", "4D Forum Voices"),
        "description": cfg.get("description", ""),
        "removal_url": cfg.get("removal_url"),
        "run": {"model": model, "prompt_version": voices.PROMPT_VERSION, "count": runs[0]["n"],
                "write_version": voices.WRITE_VERSION, "updated": runs[0]["last"]},
        "debate": cfg.get("debate") or voices.DEBATE,
        "questions": questions,
        "presets": cfg.get("presets", []),
        "entries": len(cols["ids"]),
        "withheld": withheld,
        "files": {"entries": write_json(out, "voices/entries.json", cols)},
    }


# --------------------------------------------------------------------------- output helpers

def write_json(out, rel, obj):
    """Write compact JSON; returns the relative URL with a content hash for cache busting."""
    path = out / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path.write_bytes(data)
    return f"{rel}?v={hashlib.sha1(data).hexdigest()[:10]}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(ROOT / "build" / "forum.db"))
    ap.add_argument("--out", default=str(ROOT / "site" / "data"))
    ap.add_argument("--config", default=str(ROOT / "config"))
    ap.add_argument("--embed-input", default=str(ROOT / "build" / "embed-input.jsonl"))
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cfg_site = load_json(Path(a.config) / "site.json", {})
    cfg_q = load_json(Path(a.config) / "questions.json", {})
    fr_cat = cfg_site.get("feature_request_category", 27)
    detect = lang_detector()
    con = connect_ro(a.db)

    cats = {r["id"]: dict(r) for r in con.execute("SELECT id, slug, name, parent_id FROM categories")}
    rows = con.execute(
        """SELECT t.id, t.slug, t.title, t.title_en, t.category_id, t.tags, t.created_at, t.bumped_at,
                  t.last_posted_at, t.posts_count, t.views, t.like_count, t.vote_count, t.closed, t.archived,
                  t.has_accepted_answer, t.lang, t.summary_line, t.summary_paragraph, t.enriched_at,
                  substr(t.first_post_raw, 1, 3000) AS first_post, c.slug AS category_slug
           FROM topics t LEFT JOIN categories c ON c.id = t.category_id
           ORDER BY t.id""").fetchall()

    tag_count, cat_count, lang_count = {}, {}, {}
    for r in rows:
        for tg in json.loads(r["tags"] or "[]"):
            tag_count[tg] = tag_count.get(tg, 0) + 1
    tags = sorted(tag_count, key=lambda t: (-tag_count[t], t))
    tag_index = {t: i for i, t in enumerate(tags)}

    topic_rows, paras, embed = [], [], []
    for r in rows:
        flags = (FLAG_CLOSED if r["closed"] else 0) | (FLAG_ARCHIVED if r["archived"] else 0) \
            | (FLAG_ACCEPTED if r["has_accepted_answer"] else 0)
        enriched = r["enriched_at"] is not None
        lang = r["lang"]
        if enriched:
            flags |= FLAG_ENRICHED
        if not lang:
            lang = detect(f"{r['title']}\n{r['first_post'] or ''}", r["category_slug"])
            flags |= FLAG_LANG_GUESSED
        title_en = r["title_en"] if enriched and r["title_en"] and r["title_en"] != r["title"] else None
        tlist = [tag_index[t] for t in json.loads(r["tags"] or "[]")]
        topic_rows.append([
            r["id"], r["slug"], r["title"], title_en, r["category_id"], tlist, lang,
            days(r["created_at"]), days(r["last_posted_at"] or r["bumped_at"]),
            r["posts_count"] or 0, r["views"] or 0, r["like_count"] or 0, r["vote_count"] or 0, flags,
            (r["summary_line"] or None) if enriched else None,
        ])
        paras.append((r["summary_paragraph"] or None) if enriched else None)
        cat_count[r["category_id"]] = cat_count.get(r["category_id"], 0) + 1
        lang_count[lang] = lang_count.get(lang, 0) + 1
        # embedding input (not published): English summaries when available, else title + opening text
        tag_text = ", ".join(json.loads(r["tags"] or "[]"))
        if enriched:
            text = "\n".join(x for x in (r["title_en"] or r["title"], r["summary_line"], r["summary_paragraph"],
                                          f"Tags: {tag_text}" if tag_text else "") if x)
        else:
            text = "\n".join(x for x in (r["title"], strip_markup(r["first_post"], 1200),
                                          f"Tags: {tag_text}" if tag_text else "") if x)
        embed.append({"id": r["id"], "h": hashlib.sha1(text.encode()).hexdigest()[:12], "e": int(enriched), "text": text})

    fields = ["id", "slug", "title", "title_en", "cat", "tags", "lang", "created", "active",
              "posts", "views", "likes", "votes", "flags", "line"]
    topics_file = write_json(out, "topics.json", {"fields": fields, "rows": topic_rows})
    text_file = write_json(out, "text.json", {"count": len(paras), "para": paras})

    topic_ids = {r["id"] for r in rows}
    topic_cat = {r["id"]: r["category_id"] for r in rows}
    runs, questions, default_run = build_runs(con, topic_ids, cfg_site, cfg_q, upstream_questions(), out, topic_cat, fr_cat)
    tips = build_tips(con, topic_ids, load_json(Path(a.config) / "tips.json", {}), out)
    voices = build_voices(con, topic_ids, load_json(Path(a.config) / "voices.json", {}), out)

    # keep embeddings produced by an earlier build_embeddings run (it patches the manifest itself)
    prev = load_json(out / "manifest.json", {})
    q1 = lambda sql: con.execute(sql).fetchone()[0]
    manifest = {
        "schema": 1,
        "title": cfg_site.get("title", "4D Forum Insights"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "forum_url": cfg_site.get("forum_url", "https://discuss.4d.com"),
        "feature_request_category": fr_cat,
        "stats": {
            "topics": len(rows),
            "enriched": sum(1 for r in rows if r["enriched_at"]),
            "classified": q1("SELECT COUNT(DISTINCT topic_id) FROM classifications WHERE prompt_version NOT LIKE 'tips-%' AND prompt_version NOT LIKE 'voices-%'"),
            "last_topic": max((r["created_at"] for r in rows if r["created_at"]), default=None),
            "last_fetch": q1("SELECT MAX(raw_fetched_at) FROM topics"),
        },
        "fields": fields,
        "flags": {"closed": FLAG_CLOSED, "archived": FLAG_ARCHIVED, "accepted": FLAG_ACCEPTED,
                  "enriched": FLAG_ENRICHED, "lang_guessed": FLAG_LANG_GUESSED},
        "categories": sorted(
            [{"id": cid, "slug": c["slug"], "name": c["name"], "count": cat_count.get(cid, 0)}
             for cid, c in cats.items() if cat_count.get(cid)], key=lambda c: -c["count"]),
        "tags": tags,
        "tag_counts": [tag_count[t] for t in tags],
        "languages": sorted([{"code": k, "name": LANG_NAMES.get(k, k), "count": v} for k, v in lang_count.items()],
                            key=lambda x: -x["count"]),
        "questions": questions,
        "runs": runs,
        "default_run": default_run,
        "presets": cfg_site.get("presets", []),
        "files": {"topics": topics_file, "text": text_file},
        "tips": tips,
        "voices": voices,
        "embeddings": prev.get("embeddings"),
        "embedding_config": cfg_site.get("embeddings"),
    }
    write_json(out, "manifest.json", manifest)

    classified = {r[0] for r in con.execute(
        "SELECT DISTINCT topic_id FROM classifications WHERE prompt_version NOT LIKE 'tips-%' AND prompt_version NOT LIKE 'voices-%'")}
    for e in embed:
        e["c"] = int(e["id"] in classified)
    ep = Path(a.embed_input)
    ep.parent.mkdir(parents=True, exist_ok=True)
    with ep.open("w", encoding="utf-8") as f:
        for e in embed:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    sizes = {p.relative_to(out).as_posix(): p.stat().st_size for p in sorted(out.rglob("*.json"))}
    print(f"{len(rows)} topics ({manifest['stats']['enriched']} enriched), {len(runs)} runs "
          f"(default {default_run}), {len(questions)} questions, {len(tags)} tags")
    if tips:
        print(f"  tips: {len(tips['runs'])} runs, {tips['entries']} knowledge-base entries")
    if voices:
        print(f"  voices: {voices['run']['count']} threads rated, {voices['entries']} published write-ups "
              f"({voices['withheld']} withheld)")
    for run in runs:
        for qid, b in run["bands"].items():
            print(f"  bands {run['id']} {qid}: cuts {b['cuts']} ({b['method']}, {b['reference']} reference topics)")
    for k, v in sizes.items():
        print(f"  {k:<48} {v / 1024:8.0f} KB")


if __name__ == "__main__":
    main()
