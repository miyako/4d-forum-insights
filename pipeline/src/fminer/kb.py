"""Knowledge base ("tips"): mine reusable Q&A and tips from the forum. Not part of upstream 4d-feature-miner.

Two steps, both resumable and time-boxable like the rest of the pipeline:

  kb-classify  decision model (/v1/systemone) answers two questions per enriched topic:
               area  which part of 4D the topic is about (choice, probabilities allow several areas)
               qa    solved question / open question / tip / other
               Stored in `classifications` under prompt_version "tips-…" (the export keeps these runs apart
               from the feature-request runs).
  kb-write     generative model (chat API) rewrites topics classified "solved" or "tip" into a knowledge-base
               entry: a self-contained question (solved) or headline (tip), plus a short answer. Stored in
               `kb_entries`. Only these generated texts are published, never post text.
"""

import hashlib
import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import db
from .classify import MAX_STATE_CHARS, Decider
from .enrich import LLM, _CODE_BLOCK, _QUOTE, clean
from .scrape import now, split_raw

AREAS = {
    "language": "the 4D programming language: commands, classes, objects, collections, methods, "
                "formulas, compiler, debugging code",
    "interface": "the user interface: forms, form objects, list boxes, menus, windows, events, printing",
    "database": "data and storage: ORDA, queries, entities, tables, structure, indexes, SQL, transactions, "
                "data file, journal",
    "web": "web and network: web server, REST, HTTP requests, web services, sockets, email, Qodly",
    "administration": "deployment and administration: 4D Server, client-server setup, installation, build "
                      "application, licenses, backup, upgrades, performance, operating system compatibility",
    "write_pro": "4D Write Pro documents",
    "view_pro": "4D View Pro spreadsheets",
    "third_party": "third-party plug-ins, components, libraries and integration with external tools",
    "other": "none of the above: company news, events, the forum itself, off-topic",
}

QA = {
    "solved": "a question or problem that the thread answers: a solution, workaround or clear explanation is given",
    "open": "a question or problem that the thread does not answer usefully",
    "tip": "shares reusable know-how without asking for help: a tip, how-to, code sample, explanation or resource",
    "other": "no reusable know-how: feature request, bug report without workaround, announcement, opinion or chat",
}

QUESTIONS = {
    "area": {
        "type": "choice",
        "instructions": "Which part of 4D (a database and application development platform) is this forum thread about?",
        "criteria": AREAS,
    },
    "qa": {
        "type": "choice",
        "instructions": "Does this forum thread contain reusable knowledge for other 4D developers?",
        "criteria": QA,
    },
}

PROMPT_VERSION = "tips-v1-" + hashlib.sha1(json.dumps(QUESTIONS, sort_keys=True).encode()).hexdigest()[:8]

SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_entries (
    topic_id INTEGER PRIMARY KEY,
    mode TEXT,                -- solved | tip (the qa choice the entry was written for)
    headline TEXT,            -- solved: a self-contained question; tip: a headline
    body TEXT,                -- the answer / the know-how, 1-3 sentences
    answered INTEGER,         -- the writer's own check: 0 = the thread does not really answer / teach anything
    problems TEXT,            -- JSON list of quality checks the best attempt still failed ([] = clean)
    model TEXT,
    prompt_version TEXT,
    created_at TEXT,
    error TEXT
);
"""


def connect():
    con = db.connect()
    con.executescript(SCHEMA)
    return con


# ------------------------------------------------------------------------------------------- classify

def build_state(row, max_chars=MAX_STATE_CHARS):
    body = _CODE_BLOCK.sub("[code snippet]", clean(row["first_post_en"], max_chars))
    state = f"Forum post title: {row['title_en']}\n\nForum post:\n{body}"
    replies = max(0, (row["posts_count"] or 1) - 1)
    state += f"\n\nThe thread has {replies} repl{'y' if replies == 1 else 'ies'}"
    state += ", one of them marked as the accepted answer." if row["has_accepted_answer"] else "."
    if row["summary_paragraph"]:
        state += f"\n\nSummary of the whole thread: {row['summary_paragraph']}"
    return state


class KbDecider(Decider):
    def ask(self, state):
        body = {"state": state, "questions": QUESTIONS}
        if self.model:
            body["model"] = self.model
        for attempt in range(4):
            r = self.http.post("/v1/systemone", json=body)
            if r.status_code < 500:
                break
            time.sleep(2 * (attempt + 1))
        r.raise_for_status()
        return r.json()


def classify(con, base_url, model, workers=4, limit=None, sample=None, seed=7, stale=False, budget_minutes=None,
             log=print):
    decider = KbDecider(base_url, model)
    model_id = decider.model_id()
    extra = "AND c.created_at >= COALESCE(t.enriched_at, '')" if stale else ""
    rows = con.execute(
        f"""SELECT t.* FROM topics t
            WHERE t.enriched_at IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM classifications c WHERE c.topic_id=t.id AND c.model=? AND c.prompt_version=? {extra})
            ORDER BY t.id DESC""",
        (model_id, PROMPT_VERSION),
    ).fetchall()
    if sample:
        rows = random.Random(seed).sample(rows, min(sample, len(rows)))
    if limit:
        rows = rows[:limit]
    log(f"[kb-classify] {len(rows)} topics with {model_id} ({PROMPT_VERSION})")

    def save(row, res):
        con.execute(
            """INSERT OR REPLACE INTO classifications (topic_id, model, prompt_version, answers, input_tokens, created_at)
               VALUES (?,?,?,?,?,?)""",
            (row["id"], model_id, PROMPT_VERSION, json.dumps(res["answers"]),
             res.get("usage", {}).get("input_tokens"), now()))

    _pool(con, rows, lambda row: decider.ask_fitting(lambda n: build_state(row, n)), save, workers, "kb-classify", log, budget_minutes)


# ------------------------------------------------------------------------------------------- write

WRITE_SYSTEM = (
    "You turn threads from the 4D developer forum (4D is a database and application development platform with "
    "its own programming language) into short knowledge-base entries in English. Use only what the thread says; "
    "do not speculate or add advice of your own. Be specific: name the 4D commands, classes, functions, properties, "
    "settings, versions and products involved. Do not copy code; describe what it does. Do not mention the forum, "
    "the users or the thread."
)

QUESTION_START = ("How", "Why", "What", "Which", "Where", "When", "Who", "Can", "Is", "Are", "Does", "Do", "Should",
                  "Will", "Could", "Has", "Have")
MIN_HEADLINE, MIN_BODY = 15, 50
MAX_HEADLINE_WORDS, MAX_BODY_WORDS = 28, 110

TASKS = {
    "solved": (
        "Write a knowledge-base entry for this thread.\n"
        "- question: the problem as ONE short question (at most 20 words) that a developer would type into a search "
        "box. Start with How, Why, What, Which, Where, When, Can, Is or Does. Generic: no names, no version "
        "numbers unless they matter.\n"
        "- answer: the solution, workaround or explanation that the replies give, in 1 to 3 sentences (at most 70 "
        "words). Start directly with the answer, do not restate the problem.\n"
        "- answered: false if the replies do not actually solve or explain the problem."
    ),
    "tip": (
        "Write a knowledge-base entry for the know-how shared in this thread.\n"
        "- headline: a short descriptive title (at most 12 words), not a question.\n"
        "- summary: the know-how itself in 1 to 3 sentences (at most 70 words), specific enough to be useful on "
        "its own: what to do, which command or setting, and why.\n"
        "- answered: false if the thread contains no concrete know-how (for example only a link or an announcement)."
    ),
}

SCHEMAS = {
    "solved": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "minLength": MIN_HEADLINE, "maxLength": 400},
            "answer": {"type": "string", "minLength": MIN_BODY, "maxLength": 1500},
            "answered": {"type": "boolean"},
        },
        "required": ["question", "answer", "answered"],
    },
    "tip": {
        "type": "object",
        "properties": {
            "headline": {"type": "string", "minLength": MIN_HEADLINE, "maxLength": 300},
            "summary": {"type": "string", "minLength": MIN_BODY, "maxLength": 1500},
            "answered": {"type": "boolean"},
        },
        "required": ["headline", "summary", "answered"],
    },
}
WRITE_VERSION = "kb-v2-" + hashlib.sha1(
    json.dumps([WRITE_SYSTEM, TASKS, SCHEMAS], sort_keys=True).encode()).hexdigest()[:8]

MAX_THREAD_CHARS = 6000


def thread_excerpt(thread_raw, limit=MAX_THREAD_CHARS):
    """Replies with quotes removed and code shortened (the answer is usually in the replies)."""
    def short_code(m):
        c = m.group(0)
        return c if len(c) <= 400 else c[:400] + " …[code truncated]"

    parts, total = [], 0
    for p in split_raw(thread_raw or "")[1:]:
        t = _QUOTE.sub("", clean(p["text"], 1500))
        t = _CODE_BLOCK.sub(short_code, t).strip()
        if not t:
            continue
        t = f"- {p['user']}: {t}"
        if total + len(t) > limit:
            parts.append("[…more replies truncated]")
            break
        parts.append(t)
        total += len(t)
    return "\n".join(parts)


def problems(mode, h, b):
    """Reasons an entry is not good enough (empty list = good)."""
    out = []
    if len(h) < MIN_HEADLINE or len(h.split()) > MAX_HEADLINE_WORDS:
        out.append("headline length")
    if len(b) < MIN_BODY or len(b.split()) > MAX_BODY_WORDS:
        out.append("body length")
    if not re.search(r"[.!?)`\"'\]]$", b):
        out.append("body cut off")
    if mode == "solved" and h.split(" ", 1)[0] not in QUESTION_START:
        out.append("not a question")
    return out


def write_one(llm, row, mode):
    body = _CODE_BLOCK.sub(lambda m: m.group(0)[:600], clean(row["first_post_en"], 4000))
    user = (f"Title: {row['title_en']}\n\nOriginal post:\n{body}\n\n"
            f"Replies (may be in another language):\n{thread_excerpt(row['thread_raw']) or '(none)'}")
    if row["summary_paragraph"]:
        user += f"\n\nSummary of the thread: {row['summary_paragraph']}"
    user += "\n\n" + TASKS[mode]
    keys = ("question", "answer") if mode == "solved" else ("headline", "summary")
    best, last_err = None, None
    for attempt in range(4):
        out = llm.chat(WRITE_SYSTEM, user, schema=SCHEMAS[mode], max_tokens=700, temperature=0.2 + 0.25 * attempt)
        try:
            d = json.loads(out)
            h, b, ok = d[keys[0]].strip(), d[keys[1]].strip(), bool(d.get("answered", True))
        except (json.JSONDecodeError, KeyError, AttributeError) as e:
            last_err = e
            continue
        h = re.sub(r"\s+", " ", h)
        if mode == "solved":
            h = re.sub(r"[.!:]+$", "", h)
            if not h.endswith("?"):
                h += "?"
        cand = {"headline": h, "body": b, "answered": ok, "problems": problems(mode, h, b)}
        if not cand["problems"]:
            return cand
        if best is None or len(cand["problems"]) < len(best["problems"]):
            best = cand
    if best is None:
        raise last_err or ValueError("no output")
    return best


def write(con, base_url, model, workers=4, limit=None, min_p=0.0, redo=False, budget_minutes=None, log=print):
    """Write entries for topics whose latest tips classification says solved/tip with probability >= min_p."""
    llm = LLM(base_url, model)
    model_id = llm.model_id()
    todo = "" if redo else """AND NOT EXISTS (SELECT 1 FROM kb_entries k WHERE k.topic_id=t.id AND k.headline IS NOT NULL
                                  AND k.prompt_version=? AND k.mode=x.mode AND k.created_at >= x.created_at)"""
    rows = con.execute(
        f"""WITH x AS (
                SELECT c.topic_id, json_extract(c.answers, '$.qa.choice') AS mode, c.created_at,
                       json_extract(c.answers, '$.qa.probabilities.' || json_extract(c.answers, '$.qa.choice')) AS p,
                       ROW_NUMBER() OVER (PARTITION BY c.topic_id ORDER BY c.created_at DESC) AS rn
                FROM classifications c WHERE c.prompt_version LIKE 'tips-%')
            SELECT t.id, t.title_en, t.first_post_en, t.thread_raw, t.summary_paragraph, x.mode
            FROM x JOIN topics t ON t.id = x.topic_id
            WHERE x.rn = 1 AND x.mode IN ('solved', 'tip') AND x.p >= ? {todo}
            ORDER BY t.id DESC""",
        (min_p,) if redo else (min_p, WRITE_VERSION),
    ).fetchall()
    if limit:
        rows = rows[:limit]
    log(f"[kb-write] {len(rows)} topics with {model_id} ({WRITE_VERSION})")

    def save(row, out):
        con.execute(
            """INSERT OR REPLACE INTO kb_entries (topic_id, mode, headline, body, answered, problems, model,
                                                  prompt_version, created_at, error)
               VALUES (?,?,?,?,?,?,?,?,?,NULL)""",
            (row["id"], row["mode"], out["headline"], out["body"], int(out["answered"]), json.dumps(out["problems"]),
             model_id, WRITE_VERSION, now()))

    def fail(row, err):
        con.execute("""INSERT INTO kb_entries (topic_id, mode, error, model, prompt_version, created_at)
                       VALUES (?,?,?,?,?,?) ON CONFLICT(topic_id) DO UPDATE SET error=excluded.error""",
                    (row["id"], row["mode"], err, model_id, WRITE_VERSION, now()))

    _pool(con, rows, lambda row: write_one(llm, row, row["mode"]), save, workers, "kb-write", log, budget_minutes, fail)


# ------------------------------------------------------------------------------------------- shared

def _pool(con, rows, work, save, workers, label, log, budget_minutes=None, fail=None):
    started, done, errors = time.monotonic(), 0, 0
    deadline = started + budget_minutes * 60 if budget_minutes else None

    def wrapped(row):
        if deadline and time.monotonic() > deadline:
            return row, None, "budget"
        try:
            return row, work(row), None
        except Exception as e:
            return row, None, repr(e)

    pool = ThreadPoolExecutor(workers)
    try:
        for fut in as_completed([pool.submit(wrapped, r) for r in rows]):
            row, out, err = fut.result()
            if err == "budget":
                continue
            done += 1
            if err:
                errors += 1
                log(f"[{label}] topic {row['id']}: {err}")
                if fail:
                    fail(row, err)
            else:
                save(row, out)
            con.commit()
            if done % 25 == 0 or done == len(rows):
                rate = done / (time.monotonic() - started)
                log(f"[{label}] {done}/{len(rows)}  {rate * 60:.1f}/min  eta {(len(rows) - done) / rate / 3600:.1f} h"
                    f"  errors={errors}")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if done < len(rows):
        log(f"[{label}] time budget reached: {len(rows) - done} topics left for the next run")


def stats(con, log=print):
    for r in con.execute(
            """SELECT model, prompt_version, COUNT(*) n FROM classifications WHERE prompt_version LIKE 'tips-%'
               GROUP BY 1, 2"""):
        log(f"kb-classify {r['model']} {r['prompt_version']}: {r['n']} topics")
        for q in ("qa", "area"):
            dist = con.execute(
                f"""SELECT json_extract(answers, '$.{q}.choice') k, COUNT(*) n FROM classifications
                    WHERE model=? AND prompt_version=? GROUP BY 1 ORDER BY 2 DESC""",
                (r["model"], r["prompt_version"])).fetchall()
            log(f"  {q}: " + ", ".join(f"{d['k']} {d['n']}" for d in dist))
    for r in con.execute("""SELECT mode, COUNT(headline) ok, SUM(answered = 0) unanswered,
                                   SUM(problems IS NOT NULL AND problems != '[]') flawed, COUNT(error) err
                            FROM kb_entries GROUP BY 1"""):
        log(f"kb-write {r['mode']}: {r['ok']} entries ({r['unanswered']} not answered, {r['flawed']} with flaws), "
            f"{r['err']} errors")
