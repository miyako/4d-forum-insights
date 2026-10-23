import json

from . import db

BUCKETS = ["most_unlikely", "unlikely", "neutral", "likely", "most_likely"]


def bucket_sql(score_col="c.fr_score"):
    """Map the expected level (0..4) to the nearest of the five buckets."""
    return (
        f"CASE WHEN {score_col} >= 3.5 THEN 'most_likely' WHEN {score_col} >= 2.5 THEN 'likely' "
        f"WHEN {score_col} >= 1.5 THEN 'neutral' WHEN {score_col} >= 0.5 THEN 'unlikely' ELSE 'most_unlikely' END"
    )


def default_model(con):
    row = con.execute(
        "SELECT model, prompt_version FROM classifications GROUP BY model, prompt_version ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()
    return (row["model"], row["prompt_version"]) if row else (None, None)


def stats(con):
    q = lambda sql, *a: con.execute(sql, a).fetchone()[0]
    print(f"topics listed     : {q('SELECT COUNT(*) FROM topics')}")
    print(f"  in FR category  : {q('SELECT COUNT(*) FROM topics WHERE category_id=?', db.FEATURE_REQUEST_CATEGORY)}")
    print(f"raw fetched       : {q('SELECT COUNT(*) FROM topics WHERE thread_raw IS NOT NULL')}")
    print(f"fetch errors      : {q('SELECT COUNT(*) FROM topics WHERE fetch_error IS NOT NULL')}")
    print(f"enriched          : {q('SELECT COUNT(*) FROM topics WHERE enriched_at IS NOT NULL')}")
    print(f"enrich errors     : {q('SELECT COUNT(*) FROM topics WHERE enrich_error IS NOT NULL AND enriched_at IS NULL')}")
    for r in con.execute("SELECT lang, COUNT(*) n FROM topics WHERE lang IS NOT NULL GROUP BY lang ORDER BY n DESC"):
        print(f"  lang {r['lang']:<12}: {r['n']}")
    for r in con.execute("SELECT model, prompt_version, COUNT(*) n FROM classifications GROUP BY 1,2"):
        print(f"classified        : {r['n']}  ({r['model']} / {r['prompt_version']})")


def record(row):
    """The JSON shape exposed to users."""
    return {
        "url": row["url"],
        "title": row["title"],
        "title_en": row["title_en"],
        "category": row["category_slug"],
        "is_feature_request_category": row["category_id"] == db.FEATURE_REQUEST_CATEGORY,
        "lang": row["lang"],
        "created_at": row["created_at"],
        "posts_count": row["posts_count"],
        "views": row["views"],
        "summary_line": row["summary_line"],
        "summary_paragraph": row["summary_paragraph"],
        "full_text": row["first_post_raw"],
        "full_text_en": row["first_post_en"],
        "classification": {
            "bucket": row["bucket"],
            "fr_score": row["fr_score"],
            "gap_p": row["gap_p"],
            "request_p": row["request_p"],
            "model": row["model"],
        } if "bucket" in row.keys() and row["bucket"] else None,
    }


def run(con, a):
    model, version = (a.model, None) if a.model else default_model(con)
    if not model:
        raise SystemExit("no classifications yet; run `fminer classify` first")
    if version is None:
        version = con.execute("SELECT MAX(prompt_version) FROM classifications WHERE model=?", (model,)).fetchone()[0]
    min_level = {"most_likely": 3.5, "likely": 2.5, "neutral": 1.5}[a.min_bucket]
    where = ["c.model=?", "c.prompt_version=?", "c.fr_score>=?"]
    params = [model, version, min_level]
    if not a.include_fr_category:
        where.append("t.category_id<>?")
        params.append(db.FEATURE_REQUEST_CATEGORY)
    if a.search:
        where.append("t.id IN (SELECT rowid FROM topics_fts WHERE topics_fts MATCH ?)")
        params.append(a.search)
    rows = con.execute(
        f"""SELECT t.*, cat.slug AS category_slug, c.model, c.fr_score, c.gap_p, c.request_p,
                   {bucket_sql()} AS bucket
            FROM topics t JOIN classifications c ON c.topic_id=t.id
            LEFT JOIN categories cat ON cat.id=t.category_id
            WHERE {' AND '.join(where)}
            ORDER BY c.fr_score DESC, c.request_p DESC LIMIT ?""",
        (*params, a.limit),
    ).fetchall()
    if a.json:
        print(json.dumps([record(r) for r in rows], ensure_ascii=False, indent=2))
        return
    for r in rows:
        title = r["title_en"] or r["title"]
        print(f"{r['fr_score']:.2f}  {r['bucket']:<12} {r['category_slug']:<20} {title[:70]}")
        if r["summary_line"]:
            print(f"      {r['summary_line']}")
        print(f"      {r['url']}")


def export(con, path, model=None):
    """Write every topic as one JSON record per line, with the chosen run's classification."""
    model, version = (model, None) if model else default_model(con)
    if model and version is None:
        version = con.execute("SELECT MAX(prompt_version) FROM classifications WHERE model=?", (model,)).fetchone()[0]
    rows = con.execute(
        f"""SELECT t.*, cat.slug AS category_slug, c.model, c.fr_score, c.gap_p, c.request_p,
                   CASE WHEN c.fr_score IS NULL THEN NULL ELSE {bucket_sql()} END AS bucket
            FROM topics t LEFT JOIN categories cat ON cat.id=t.category_id
            LEFT JOIN classifications c ON c.topic_id=t.id AND c.model=? AND c.prompt_version=?
            ORDER BY t.id""",
        (model, version),
    )
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(record(r), ensure_ascii=False) + "\n")
            n += 1
    print(f"wrote {n} records to {path}")
