"""Private decision-only corpus ratings. The attached source is always read-only."""

from collections import Counter
import argparse
from copy import deepcopy
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import httpx

from . import questions
from .scrape import now, split_raw

EVIDENCE_VERSION = "questions-corpus-e3"
QUESTIONS = deepcopy(questions.QUESTIONS)
QUESTIONS["has_question"]["instructions"] += (
    " Judge the ORIGINAL ASKER'S opening need, using the title and opening prose. "
    "A release announcement, shared resource or social/feature teaser is not a help question merely because "
    "another participant asks an incidental question later. Do not infer a question hidden in an unavailable image."
)
QUESTIONS["impact"]["instructions"] += (
    " Repeated application/client termination on users' machines is evidenced operational disruption and "
    "can be significant without a quantified loss or a production-outage claim. Distinguish that actual recurring "
    "disruption from a generic crash mention, a suspected risk or a capability question with no stated consequences."
)
PROMPT_VERSION = "questions-corpus-p1-" + hashlib.sha1(
    json.dumps([questions.EVIDENCE_VERSION, QUESTIONS], sort_keys=True).encode()).hexdigest()[:8]


class CorpusDecider(questions.QuestionsDecider):
    questions = QUESTIONS


SCHEMA = """
CREATE TABLE IF NOT EXISTS corpus_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS questions_decisions (
    topic_id INTEGER PRIMARY KEY,
    lang TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    readiness_error TEXT,
    evidence_warnings TEXT NOT NULL,
    solved INTEGER,
    closed INTEGER,
    replies INTEGER,
    answers TEXT,
    decision_input_hash TEXT,
    decision_model TEXT,
    model_revision TEXT,
    decision_version TEXT,
    evidence_version TEXT,
    classified_at TEXT,
    decision_error TEXT,
    evidence_budget INTEGER,
    evidence_posts TEXT,
    elapsed_seconds REAL
);
CREATE TABLE IF NOT EXISTS questions_attempts (
    id INTEGER PRIMARY KEY,
    topic_id INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    decision_model TEXT NOT NULL,
    model_revision TEXT NOT NULL,
    decision_version TEXT NOT NULL,
    evidence_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    elapsed_seconds REAL,
    outcome TEXT NOT NULL,
    error TEXT,
    evidence_budget INTEGER
);
"""


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_hash(rows):
    # SQLite writes can remain in a WAL; hashing the main file alone misses those edits.
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(dict(row), sort_keys=True).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def connect(source, results):
    source, results = Path(source).resolve(), Path(results).resolve()
    if source == results or (results.exists() and source.exists() and source.samefile(results)):
        raise ValueError("source and results must be different databases")
    if not source.is_file():
        raise ValueError(f"source database does not exist: {source}")
    results.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(results, timeout=60)
    con.row_factory = sqlite3.Row
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if tables & {"topics", "classifications", "questions_pilot", "kb_entries", "voices_entries"}:
        con.close()
        raise ValueError("results must be an isolated ratings DB, not a working/source/pilot DB")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    con.executescript(SCHEMA)
    con.execute("ATTACH DATABASE ? AS source", (source.as_uri() + "?mode=ro",))
    return con


def get_state(con, key):
    row = con.execute("SELECT value FROM corpus_state WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def set_state(con, key, value):
    con.execute("INSERT INTO corpus_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, sort_keys=True)))


def prepared(row):
    """Reuse fresh English openings; originals, never summaries, are the fallback."""
    data = dict(row)
    fresh = bool(data["enriched_at"] and not data["enrich_error"] and data["first_post_en"]
                 and (not data["raw_changed_at"] or data["enriched_at"] >= data["raw_changed_at"]))
    posts = split_raw(data["thread_raw"] or "")
    if not fresh:
        data["first_post_en"] = posts[0]["text"] if posts else ""
        data["title_en"] = None
    return data, fresh


def fingerprint(row):
    data, fresh = prepared(row)
    return hashlib.sha256(json.dumps(
        [questions.input_hash(data), fresh, row["raw_changed_at"], EVIDENCE_VERSION],
        sort_keys=True).encode()).hexdigest()


def readiness(row):
    posts = split_raw(row["thread_raw"] or "")
    if not posts or posts[0]["n"] != 1 or not posts[0]["text"].strip():
        return "missing or unparseable original opening/thread", []
    data, fresh = prepared(row)
    warnings = ["code replaced by placeholders; code-dependent understanding is limited"]
    if not fresh:
        warnings.append("fresh English opening unavailable; using original-language opening")
    if row["fetch_error"]:
        warnings.append("source fetch error recorded; available evidence may be incomplete")
    if row["posts_count"] is None:
        warnings.append("reply count unknown")
    elif len(posts) < row["posts_count"]:
        warnings.append("some listed posts unavailable")
    return None, warnings


def evidence(row, budget):
    data, fresh = prepared(row)
    prefix = (f"Source language: {row['lang'] or 'unknown'}. Opening: "
              f"{'existing English enrichment' if fresh else 'original language'}; "
              "replies retain their original languages.\n"
              "Code is replaced by placeholders: code-dependent details may be unavailable.\n")
    if budget >= 3600:
        prefix += ("Anchor question presence and coverage to the original asker's opening need, not an incidental "
                   "question by a later participant. The asker can supply their own working solution in a reply.\n")
    if budget >= 3600 and fresh and row["summary_paragraph"]:
        posts = split_raw(row["thread_raw"])
        _, scrub = questions.anonymise(posts)
        summary = scrub(row["summary_paragraph"])
        prefix += ("Existing English enrichment summary helps interpret multilingual replies; it is secondary "
                   "context, may omit details or misattribute speakers, and is NOT acceptance evidence. "
                   "Numbered original posts control the decision:\n"
                   + questions.bounded_excerpt(summary, 450 if budget >= 6000 else 200) + "\n")
    text, cited = questions.evidence(data, budget - len(prefix))
    if row["posts_count"] is None:
        text = text.replace("replies: None ", "replies: unknown ")
    return prefix + text, cited


def prepare(con, source, scope="all", published_manifest=None, published_ids=None):
    if scope != "all":
        raise ValueError("corpus scope must explicitly be all; use topic IDs to bound execution, not membership")
    previous = get_state(con, "scope")
    if previous and previous != {"scope": scope}:
        raise ValueError("corpus membership scope changed; use a different results DB")
    rows = con.execute("SELECT * FROM source.topics ORDER BY id").fetchall()
    if not rows:
        raise ValueError("source has no topics")
    ids = {r["id"] for r in rows}
    existing = {r[0] for r in con.execute("SELECT topic_id FROM questions_decisions")}
    if existing - ids:
        raise ValueError("source lost persisted topic IDs; use a different results DB")
    comparison = "not supplied; current published corpus completeness is unknown"
    if (published_manifest is None) != (published_ids is None):
        raise ValueError("supply both published manifest and IDs for a completeness comparison")
    if published_ids is not None:
        public_ids = set(published_ids)
        expected = published_manifest["stats"]["topics"]
        if len(public_ids) != expected:
            raise ValueError("published ID count does not match the manifest")
        comparison = {
            "generated_at": published_manifest["generated_at"], "topics": expected,
            "missing_source_ids": sorted(public_ids - ids), "source_only_ids": sorted(ids - public_ids),
            "note": "ID agreement establishes membership only, not freshness of every post",
        }
    languages, ready, warnings_count = Counter(), Counter(), Counter()
    reused, originals = Counter(), Counter()
    missing = []
    incomplete = []
    unknown_replies = []
    for row in rows:
        language = row["lang"] or "unknown"
        error, warnings = readiness(row)
        languages[language] += 1
        if error:
            missing.append(row["id"])
        else:
            ready[language] += 1
            _, fresh = prepared(row)
            (reused if fresh else originals)[language] += 1
        if "some listed posts unavailable" in warnings:
            incomplete.append(row["id"])
        if row["posts_count"] is None:
            unknown_replies.append(row["id"])
        warnings_count.update(warnings)
        con.execute(
            """INSERT INTO questions_decisions(topic_id,lang,input_hash,readiness_error,evidence_warnings,
               solved,closed,replies) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(topic_id) DO UPDATE SET lang=excluded.lang,
               input_hash=excluded.input_hash,readiness_error=excluded.readiness_error,
               evidence_warnings=excluded.evidence_warnings,solved=excluded.solved,
               closed=excluded.closed,replies=excluded.replies""",
            (row["id"], language, fingerprint(row), error, json.dumps(warnings),
             row["has_accepted_answer"], row["closed"], questions.reply_count(row)))
    report = {
        "selected_at": now(), "source_path": str(Path(source).resolve()), "source_sha256": file_hash(source),
        "source_content_sha256": content_hash(rows),
        "topics": len(rows), "languages": dict(languages), "ready": dict(ready),
        "english_openings_reused": dict(reused), "original_opening_fallback": dict(originals),
        "incomplete_thread_ids": incomplete, "unknown_reply_count_ids": unknown_replies,
        "missing_evidence_ids": missing, "warnings": dict(warnings_count),
        "max_listed_at": max((r["listed_at"] or "" for r in rows), default=""),
        "max_raw_fetched_at": max((r["raw_fetched_at"] or "" for r in rows), default=""),
        "max_activity": max((r["last_posted_at"] or r["bumped_at"] or "" for r in rows), default=""),
        "published_comparison": comparison,
    }
    set_state(con, "scope", {"scope": scope})
    set_state(con, "source", report)
    con.commit()
    return report


def current(row, model, revision):
    return bool(row["answers"] and not row["decision_error"] and not row["readiness_error"]
                and row["input_hash"] == row["decision_input_hash"]
                and row["decision_model"] == model and row["model_revision"] == revision
                and row["decision_version"] == PROMPT_VERSION
                and row["evidence_version"] == EVIDENCE_VERSION)


def safe_error(error):
    # HTTP exception bodies and model responses may contain private evidence.
    detail = f"HTTP {error.response.status_code}" if isinstance(error, httpx.HTTPStatusError) else (
        "timeout" if isinstance(error, httpx.TimeoutException) else
        "invalid decision response" if isinstance(error, (ValueError, KeyError, TypeError)) else "request failed")
    return f"{type(error).__name__}: {detail} (private response details withheld)"


def classify(con, base_url, model, revision, limit=None, budget_minutes=None, topic_ids=None, log=print):
    if not get_state(con, "source"):
        raise ValueError("prepare the explicit all-language source scope first")
    if not revision.strip():
        raise ValueError("a model revision/weight fingerprint is required")
    if (limit is not None and limit < 1) or (budget_minutes is not None and budget_minutes <= 0):
        raise ValueError("execution limits and budgets must be positive")
    source = get_state(con, "source")
    if content_hash(con.execute("SELECT * FROM source.topics ORDER BY id")) != source["source_content_sha256"]:
        raise RuntimeError("source snapshot changed; prepare again to invalidate stale ratings")
    # A single writer/worker retains the full 16k server context and predictable memory use.
    decider = CorpusDecider(base_url, model)
    model_id = decider.model_id()
    set_state(con, "runtime", {"model": model_id, "revision": revision, "prompt": PROMPT_VERSION,
                             "evidence": EVIDENCE_VERSION, "workers": 1, "base_url": base_url})
    con.execute(
        """UPDATE questions_attempts SET outcome='interrupted',finished_at=?,
           error='previous process ended before saving a decision; retrying is safe' WHERE outcome='running'""",
        (now(),))
    con.commit()
    rows = con.execute("SELECT * FROM questions_decisions ORDER BY topic_id").fetchall()
    if topic_ids is not None:
        selected = set(topic_ids)
        unknown = selected - {r["topic_id"] for r in rows}
        if unknown:
            raise ValueError(f"requested topics are not in the source: {sorted(unknown)}")
        rows = [r for r in rows if r["topic_id"] in selected]
    pending = [r for r in rows if not r["readiness_error"] and not current(r, model_id, revision)]
    if limit:
        pending = pending[:limit]
    log(f"[questions-corpus] {len(pending)} scheduled; decision-only, one worker, {model_id}, {revision}")
    start = time.monotonic()
    done = failures = 0
    for rating in pending:
        if budget_minutes is not None and time.monotonic() - start >= budget_minutes * 60:
            break
        row = con.execute("SELECT * FROM source.topics WHERE id=?", (rating["topic_id"],)).fetchone()
        if fingerprint(row) != rating["input_hash"]:
            raise RuntimeError("source changed since preparation; stop and prepare again")
        began = time.monotonic()
        cursor = con.execute(
            """INSERT INTO questions_attempts(topic_id,input_hash,decision_model,model_revision,
               decision_version,evidence_version,started_at,outcome) VALUES (?,?,?,?,?,?,?,'running')""",
            (row["id"], rating["input_hash"], model_id, revision, PROMPT_VERSION, EVIDENCE_VERSION, now()))
        attempt_id = cursor.lastrowid
        con.commit()
        supplied = {}

        def build(n):
            text, cited = evidence(row, n)
            supplied.update(budget=n, posts=sorted(cited))
            return text

        try:
            result = questions.display_answers(decider.ask_fitting(build)["answers"])
        except Exception as error:
            elapsed = time.monotonic() - began
            message = safe_error(error)
            con.execute("UPDATE questions_decisions SET decision_error=? WHERE topic_id=?", (message, row["id"]))
            con.execute(
                """UPDATE questions_attempts SET outcome='failed',error=?,finished_at=?,elapsed_seconds=?,
                   evidence_budget=? WHERE id=?""",
                (message, now(), elapsed, supplied.get("budget"), attempt_id))
            con.commit()
            failures += 1
            log(f"[questions-corpus] topic {row['id']} {message}; persisted attempt {attempt_id}")
            continue
        elapsed = time.monotonic() - began
        con.execute(
            """UPDATE questions_decisions SET answers=?,decision_input_hash=?,decision_model=?,model_revision=?,
               decision_version=?,evidence_version=?,classified_at=?,decision_error=NULL,evidence_budget=?,
               evidence_posts=?,elapsed_seconds=? WHERE topic_id=?""",
            (json.dumps(result), rating["input_hash"], model_id, revision, PROMPT_VERSION, EVIDENCE_VERSION,
             now(), supplied["budget"], json.dumps(supplied["posts"]), elapsed, row["id"]))
        con.execute(
            """UPDATE questions_attempts SET outcome='completed',finished_at=?,elapsed_seconds=?,evidence_budget=?
               WHERE id=?""", (now(), elapsed, supplied["budget"], attempt_id))
        con.commit()
        done += 1
        rate = done * 60 / (time.monotonic() - start)
        log(f"[questions-corpus] topic {row['id']} ({rating['lang']}) saved; {done}/{len(pending)}; "
            f"{rate:.2f} topics/min; errors={failures}")
    elapsed = time.monotonic() - start
    run = {"finished_at": now(), "completed": done, "failures": failures, "seconds": elapsed,
           "topics_per_minute": done * 60 / elapsed if elapsed else 0, "scheduled": len(pending)}
    set_state(con, "last_run", run)
    con.commit()
    log(json.dumps(progress(con), sort_keys=True))
    if failures:
        raise RuntimeError(f"{failures} corpus decision failures persisted; rerun to retry")
    return run


def progress(con):
    runtime = get_state(con, "runtime")
    counts = Counter()
    languages = {}
    for row in con.execute("SELECT * FROM questions_decisions"):
        state = ("blocked" if row["readiness_error"] else "failed" if row["decision_error"] else
                 "current" if runtime and current(row, runtime["model"], runtime["revision"]) else "pending")
        counts[state] += 1
        languages.setdefault(row["lang"], Counter())[state] += 1
    return {"counts": dict(counts), "languages": {k: dict(v) for k, v in languages.items()},
            "source": get_state(con, "source"), "runtime": runtime, "last_run": get_state(con, "last_run"),
            "unfinished_attempts": con.execute(
                "SELECT COUNT(*) FROM questions_attempts WHERE outcome='running'").fetchone()[0]}


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "classify", "stats"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--scope", choices=("all",))
    parser.add_argument("--published-manifest", type=Path)
    parser.add_argument("--published-ids", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--model", default="")
    parser.add_argument("--model-revision", default="")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--budget-minutes", type=float)
    parser.add_argument("--topic-ids", help="comma-separated bounded check; does not change all-language membership")
    args = parser.parse_args(argv)
    if args.command == "stats" and not args.results.is_file():
        parser.error("prepare the corpus results DB before reading stats")
    with connect(args.source, args.results) as con:
        if args.command == "stats":
            print(json.dumps(progress(con), indent=2))
            return
        with Path(str(args.results) + ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                parser.error("another corpus process owns this results DB")
            if args.command == "prepare":
                if args.scope != "all":
                    parser.error("prepare requires --scope all")
                manifest = json.loads(args.published_manifest.read_text()) if args.published_manifest else None
                ids = json.loads(args.published_ids.read_text()) if args.published_ids else None
                print(json.dumps(prepare(con, args.source, args.scope, manifest, ids), indent=2))
            else:
                ids = [int(n) for n in args.topic_ids.split(",")] if args.topic_ids else None
                classify(con, args.base_url, args.model, args.model_revision, args.limit, args.budget_minutes, ids)
