import argparse
import sys


def main() -> None:
    ap = argparse.ArgumentParser(prog="fminer", description="Mine feature requests from discuss.4d.com")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="sync topic metadata from category listings")
    p.add_argument("--full", action="store_true", help="walk every page (resumable) instead of stopping at unchanged pages")

    p = sub.add_parser("fetch", help="download raw thread text for new/updated topics")
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--interval", type=float, default=1.0, help="min seconds between requests (global)")
    p.add_argument("--limit", type=int)

    p = sub.add_parser("enrich", help="detect language, translate, summarise (OpenAI-compatible LLM)")
    p.add_argument("--base-url", default="http://127.0.0.1:8081/v1")
    p.add_argument("--model", default="")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--limit", type=int)
    p.add_argument("--redo", action="store_true")

    p = sub.add_parser("classify", help="score topics with a decision model via /v1/systemone")
    p.add_argument("--base-url", default="http://127.0.0.1:8080")
    p.add_argument("--model", default="", help="model id (router mode); also the key stored in the DB")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--limit", type=int)
    p.add_argument("--sample", type=int, help="classify a random sample of N topics (half from the FR category)")
    p.add_argument("--original", action="store_true", help="use original-language text instead of the English translation")

    p = sub.add_parser("calibrate", help="evaluate a model run against the feature-request category")
    p.add_argument("--model")

    p = sub.add_parser("query", help="list likely feature requests posted outside the FR category")
    p.add_argument("--model")
    p.add_argument("--min-bucket", default="likely", choices=["most_likely", "likely", "neutral"])
    p.add_argument("--include-fr-category", action="store_true")
    p.add_argument("--search", help="FTS5 query, e.g. 'write AND pro'")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true", help="emit JSON records instead of a table")

    p = sub.add_parser("export", help="write all topics as JSON Lines")
    p.add_argument("path", nargs="?", default="data/topics.jsonl")
    p.add_argument("--model")

    sub.add_parser("index", help="rebuild the full-text index")
    sub.add_parser("stats", help="show progress")

    a = ap.parse_args()
    from . import db

    con = db.connect()

    if a.cmd == "list":
        from . import scrape
        scrape.list_topics(scrape.Client(), con, full=a.full)
    elif a.cmd == "fetch":
        from . import scrape
        scrape.fetch_raw(scrape.Client(min_interval=a.interval), con, workers=a.workers, limit=a.limit)
    elif a.cmd == "enrich":
        from . import enrich
        enrich.run(con, a.base_url, a.model, workers=a.workers, limit=a.limit, redo=a.redo)
    elif a.cmd == "classify":
        from . import classify
        classify.run(con, a.base_url, a.model, workers=a.workers, limit=a.limit, sample=a.sample, original=a.original)
    elif a.cmd == "calibrate":
        from . import classify
        classify.calibrate(con, a.model)
    elif a.cmd == "query":
        from . import query
        query.run(con, a)
    elif a.cmd == "export":
        from . import query
        query.export(con, a.path, a.model)
    elif a.cmd == "index":
        db.rebuild_fts(con)
    elif a.cmd == "stats":
        from . import query
        query.stats(con)
    sys.stdout.flush()
