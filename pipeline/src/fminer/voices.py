"""Forum Voices: threads where the community weighed in. Not part of upstream 4d-feature-miner.

  voices-classify  decision model (/v1/systemone) rates candidate threads (many participants, several of them
                   posting repeatedly) on five questions:
                   exchange help / views / news / social (Voices shows "views" and "news")
                   debate   how far apart the participants' views are (score, 5 levels)
                   settled  consensus / decided by 4D / unresolved / nothing to settle
                   subject  technical / product direction / business / community
                   heated   the discussion turns personal or emotional (internal: used to rank, never shown)
                   Stored in `classifications` under prompt_version "voices-…" (kept out of the main runs).

  voices-write     generative model (chat API) writes a neutral, anonymous write-up of each selected thread
                   (exchange "views"/"news" and debate >= MIN_DEBATE): the question discussed, 2-4 positions and the
                   outcome. Stored in `voices_entries`; entries that still name someone are never published.

Kev sees the thread title, counts, the English opening post and summary, and a spread of replies in their
original language with participants replaced by letters (A = the author). Usernames are never sent to a
generative model or published.
"""

import collections
import hashlib
import json
import random
import re
from datetime import datetime

from . import db
from .classify import MAX_STATE_CHARS, Decider
from .enrich import LLM, _CODE_BLOCK, _QUOTE, clean
from .kb import _pool
from .scrape import now, split_raw

DEBATE = [
    "no disagreement: questions, answers, thanks or agreement only",
    "minor differences of opinion",
    "a real disagreement between some participants",
    "a clear debate with several opposing views",
    "a strong, sustained debate with many participants taking sides",
]

EXCHANGE = {
    "help": "participants help someone with a specific problem or question (troubleshooting, how-to)",
    "views": "participants share and compare opinions or experiences about how 4D works, should work or should be used",
    "news": "participants react to an announcement, release or news",
    "social": "greetings, thanks, events or other social conversation",
}

SETTLED = {
    "consensus": "the participants converge on a shared view or solution",
    "decided": "4D (the company or its staff) gives an official answer or decision that closes the question",
    "unresolved": "views remain divided or the question stays open",
    "none": "there is no disagreement to settle",
}

SUBJECTS = {
    "technical": "how 4D works or should be used: language, behaviour, performance, best practices",
    "product": "the direction of the 4D product: roadmap, new or removed features, deprecations, priorities",
    "business": "4D the company and its commercial terms: licensing, pricing, support, ownership, communication",
    "community": "people and community life: greetings, events, retirements, the forum itself",
}

QUESTIONS = {
    "exchange": {
        "type": "choice",
        "instructions": "What kind of exchange is this thread from the forum of 4D (a database and application "
                        "development platform)?",
        "criteria": EXCHANGE,
    },
    "debate": {
        "type": "score",
        "instructions": "How far apart are the opinions of the participants? Working together to troubleshoot a "
                        "problem is not a disagreement.",
        "criteria": DEBATE,
    },
    "settled": {
        "type": "choice",
        "instructions": "How does the discussion end?",
        "criteria": SETTLED,
    },
    "subject": {
        "type": "choice",
        "instructions": "What is the discussion mainly about?",
        "criteria": SUBJECTS,
    },
    "heated": {
        "type": "noul",
        "instructions": "Does the discussion become personal, emotional or heated?",
    },
}

PROMPT_VERSION = "voices-v2-" + hashlib.sha1(json.dumps(QUESTIONS, sort_keys=True).encode()).hexdigest()[:8]

MIN_POSTS = 6
MIN_PARTICIPANTS = 5
MIN_RETURNING = 2      # participants with at least RETURNING_POSTS posts: a back-and-forth, not a round of replies
RETURNING_POSTS = 3
REPLY_CHARS = 260


def thread_stats(posts):
    per_user = collections.Counter(p["user"] for p in posts)
    dates = [p["date"][:10] for p in posts]
    days = (datetime.fromisoformat(max(dates)) - datetime.fromisoformat(min(dates))).days if dates else 0
    return {
        "posts": len(posts),
        "participants": len(per_user),
        "returning": sum(1 for c in per_user.values() if c >= RETURNING_POSTS),
        "days": days,
    }


def _label(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def anonymise(posts):
    """Map users to letters in order of appearance (A = the author) and replace @mentions of them."""
    labels = {}
    for p in posts:
        labels.setdefault(p["user"], _label(len(labels)))
    if not labels:
        return labels, lambda t: t
    names = sorted(labels, key=len, reverse=True)
    pattern = re.compile(r"@?\b(" + "|".join(re.escape(n) for n in names) + r")\b", re.I)
    lower = {n.lower(): v for n, v in labels.items()}
    return labels, lambda t: pattern.sub(lambda m: lower.get(m.group(1).lower(), m.group(0)), t)


def _reply(text, scrub):
    t = _QUOTE.sub("", clean(text, 2000))
    t = _CODE_BLOCK.sub("[code]", t)
    t = scrub(re.sub(r"\s+", " ", re.sub(r"-{5,}", " ", t)).strip())
    if not re.search(r"\w", t):
        return ""
    return t if len(t) <= REPLY_CHARS else t[:REPLY_CHARS].rsplit(" ", 1)[0] + " …"


def _spread(n, k):
    """k indices out of range(n), evenly spread and always including the last one."""
    if n <= k:
        return list(range(n))
    return sorted({round(i * (n - 1) / (k - 1)) for i in range(k)}) if k > 1 else [n - 1]


def build_state(row, max_chars=MAX_STATE_CHARS):
    posts = split_raw(row["thread_raw"] or "")
    st = thread_stats(posts)
    labels, scrub = anonymise(posts)
    head = [
        f"Forum thread title: {row['title_en'] or row['title']}",
        f"The thread has {st['posts']} posts by {st['participants']} participants over {st['days']} days; "
        f"{st['returning']} participants posted {RETURNING_POSTS} times or more.",
        "Opening post by participant A (English):\n"
        + scrub(_CODE_BLOCK.sub("[code]", clean(row["first_post_en"], int(max_chars * 0.25)))),
    ]
    if row["summary_paragraph"]:
        head.append(f"Summary of the whole thread: {scrub(row['summary_paragraph'])}")
    state = "\n\n".join(head)
    replies = posts[1:]
    room = max(0, max_chars - len(state) - 80)
    picked = _spread(len(replies), max(1, room // (REPLY_CHARS + 8)))
    lines, prev = [], -1
    for i in picked:
        if i > prev + 1:
            lines.append(f"[… {i - prev - 1} replies skipped]")
        t = _reply(replies[i]["text"], scrub)
        if t:
            lines.append(f"- {labels[replies[i]['user']]}: {t}")
        prev = i
    if replies:
        state += "\n\nReplies (original language, shortened):\n" + "\n".join(lines)
    return state


class VoicesDecider(Decider):
    questions = QUESTIONS


def candidates(con, min_participants=MIN_PARTICIPANTS, min_returning=MIN_RETURNING, extra=""):
    rows = con.execute(
        f"""SELECT t.* FROM topics t WHERE t.enriched_at IS NOT NULL AND t.thread_raw IS NOT NULL
            AND t.posts_count >= ? {extra} ORDER BY t.id DESC""", (MIN_POSTS,)).fetchall()
    out = []
    for r in rows:
        st = thread_stats(split_raw(r["thread_raw"]))
        if st["participants"] >= min_participants and st["returning"] >= min_returning:
            out.append(r)
    return out


def classify(con, base_url, model, workers=4, limit=None, sample=None, seed=7, stale=False, budget_minutes=None,
             min_participants=MIN_PARTICIPANTS, min_returning=MIN_RETURNING, log=print):
    decider = VoicesDecider(base_url, model)
    model_id = decider.model_id()
    newer = "AND c.created_at >= COALESCE(t.enriched_at, '')" if stale else ""
    rows = candidates(con, min_participants, min_returning, extra=f"""AND NOT EXISTS (
        SELECT 1 FROM classifications c WHERE c.topic_id=t.id AND c.model='{model_id.replace("'", "''")}'
        AND c.prompt_version='{PROMPT_VERSION}' {newer})""")
    if sample:
        rows = random.Random(seed).sample(rows, min(sample, len(rows)))
    if limit:
        rows = rows[:limit]
    log(f"[voices-classify] {len(rows)} topics with {model_id} ({PROMPT_VERSION})")

    def ask(row):
        return decider.ask_fitting(lambda n: build_state(row, n))

    def save(row, res):
        con.execute(
            """INSERT OR REPLACE INTO classifications (topic_id, model, prompt_version, answers, input_tokens, created_at)
               VALUES (?,?,?,?,?,?)""",
            (row["id"], model_id, PROMPT_VERSION, json.dumps(res["answers"]),
             res.get("usage", {}).get("input_tokens"), now()))

    _pool(con, rows, ask, save, workers, "voices-classify", log, budget_minutes)


def rank(a):
    if "exchange" not in a:
        return a["debate"]["score"]
    p = a["exchange"]["probabilities"]
    return a["debate"]["score"] * (p.get("views", 0) + p.get("news", 0))


def stats(con, top=0, log=print):
    for r in con.execute(
            """SELECT model, prompt_version, COUNT(*) n FROM classifications WHERE prompt_version = ?
               GROUP BY 1, 2""", (PROMPT_VERSION,)):
        log(f"voices-classify {r['model']} {r['prompt_version']}: {r['n']} topics")
        rows = con.execute(
            """SELECT c.topic_id, c.answers, t.title_en, t.title, t.posts_count FROM classifications c
               JOIN topics t ON t.id = c.topic_id WHERE c.model=? AND c.prompt_version=?""",
            (r["model"], r["prompt_version"])).fetchall()
        parsed = [(x, json.loads(x["answers"])) for x in rows]
        levels = collections.Counter(round(a["debate"]["score"]) for _, a in parsed)
        log("  debate: " + ", ".join(f"{k} {levels[k]}" for k in sorted(levels)))
        for q in ("exchange", "settled", "subject"):
            c = collections.Counter(a[q]["choice"] for _, a in parsed)
            log(f"  {q}: " + ", ".join(f"{k} {n}" for k, n in c.most_common()))
        log(f"  heated (p >= 0.5): {sum(a['heated']['noul'] >= 0.5 for _, a in parsed)}")
        if top:
            log(f"  top {top} by rank (debate score x P(views or news)):")
            for x, a in sorted(parsed, key=lambda p: -rank(p[1]))[:top]:
                log(f"  {rank(a):.2f} {a['debate']['score']:.2f} {a['exchange']['choice']:<6} "
                    f"{a['settled']['choice']:<10} {a['subject']['choice']:<9} "
                    f"heated {a['heated']['noul']:.2f}  #{x['topic_id']} {x['title_en'] or x['title']} "
                    f"({x['posts_count']} posts)")


# ------------------------------------------------------------------------------------------- write

MIN_DEBATE = 0.75       # "minor differences of opinion" or more
SHOWN = ("views", "news")


def selected(a):
    """Threads that get a write-up and a place on /voices/: an exchange of views (or reactions to news) with at
    least some difference of opinion."""
    return a.get("exchange", {}).get("choice") in SHOWN and a["debate"]["score"] >= MIN_DEBATE


SCHEMA = """
CREATE TABLE IF NOT EXISTS voices_entries (
    topic_id INTEGER PRIMARY KEY,
    question TEXT,            -- the issue discussed, as one neutral question
    positions TEXT,           -- JSON list of 2-4 viewpoints, anonymous
    outcome TEXT,             -- how the discussion ended
    discussion INTEGER,       -- the writer's own check: 0 = no real exchange of views
    problems TEXT,            -- JSON list of checks the best attempt still failed ([] = clean); "names" is never published
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


WRITE_SYSTEM = (
    "You write short, neutral and anonymous summaries of community discussions on the 4D developer forum (4D is a "
    "database and application development platform with its own programming language). Participants are shown as "
    "letters (A is the author of the thread). Never name or identify anyone: do not use names, usernames or the "
    "letters; say 'the author', 'some participants', 'others', or 'a 4D representative' when the thread makes clear "
    "that someone speaks for 4D. Report the views fairly, as views ('some argue that …'), without taking sides, "
    "judging people or adding opinions of your own. Do not quote or copy posts. Write in English."
)

WRITE_TASK = (
    "Summarise this discussion.\n"
    "- question: the issue the participants discuss, as ONE neutral question (at most 20 words).\n"
    "- positions: the 2 to 4 main distinct views or experiences, one sentence each (at most 30 words), without "
    "names or letters.\n"
    "- outcome: how the discussion ends, in 1 or 2 sentences (at most 45 words): a shared conclusion, a decision "
    "or answer from 4D, or views that remain divided.\n"
    "- discussion: false if the participants do not really exchange different views or experiences (for example "
    "they only help with a problem, thank someone, or react to an announcement without discussing it)."
)

WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "minLength": 15, "maxLength": 300},
        "positions": {"type": "array", "minItems": 1, "maxItems": 4,
                      "items": {"type": "string", "minLength": 15, "maxLength": 400}},
        "outcome": {"type": "string", "minLength": 20, "maxLength": 600},
        "discussion": {"type": "boolean"},
    },
    "required": ["question", "positions", "outcome", "discussion"],
}
WRITE_VERSION = "voices-w1-" + hashlib.sha1(
    json.dumps([WRITE_SYSTEM, WRITE_TASK, WRITE_SCHEMA], sort_keys=True).encode()).hexdigest()[:8]

WRITE_THREAD_CHARS = 9000
WRITE_REPLY_CHARS = 500

_GREETING = (r"regards|best regards|kind regards|cheers|thanks|thank you|many thanks|best|cordialement|bien à vous|"
             r"merci|salutations|amicalement|saludos|un saludo|gracias|grüße|gruß|gruss|viele grüße|mfg|ciao|"
             r"saluti|bye|cdlt|bonne journée|a\+|hth")
_NAME = r"[A-ZÀ-Ý][\w'’.-]+(?:\s+[A-ZÀ-Ý][\w'’.-]+){0,2}"
_SIGN_OFF = re.compile(rf"(?:^|\n)\s*(?i:{_GREETING})\b[\s,.!:;-]*({_NAME})\s*$")
_NAME_LINE = re.compile(rf"\n\s*-*\s*({_NAME})\s*$")
_WORD = re.compile(r"[A-Za-zÀ-ÿ]{3,}")
# capitalised words that are not names even when someone's username or sign-off contains them
NOT_NAMES = {
    "the", "and", "for", "you", "all", "best", "thanks", "regards", "cheers", "merci", "team", "support", "admin",
    "server", "data", "dev", "developer", "web", "form", "forms", "write", "view", "pro", "code", "app", "apps",
    "soft", "software", "tech", "info", "sales", "france", "usa", "german", "germany", "japan", "spain", "italia",
    "consulting", "systems", "system", "solutions", "group", "net", "mac", "windows", "user", "users", "qodly",
    "orda", "rest", "sql", "http", "json", "api", "net", "digital", "service", "services", "design", "product",
}


def _sign_off_names(text):
    """Names in a post's sign-off ("Regards, Chuck")."""
    m = _SIGN_OFF.search((text or "").strip()[-160:])
    return set(_WORD.findall(m.group(1))) if m else set()


def _designature(text):
    """Drop a trailing sign-off ("Regards, Chuck") or a last line with only a name, so that names in signatures
    do not reach the writer."""
    t = (text or "").rstrip()
    for _ in range(2):
        m = _SIGN_OFF.search(t, max(0, len(t) - 160)) or _NAME_LINE.search(t, max(0, len(t) - 60))
        if not m:
            break
        t = t[:m.start()].rstrip()
    return t


def forbidden_names(posts, title=""):
    """Words that would identify a participant: parts of usernames and sign-off names (lower case)."""
    words = set()
    for p in posts:
        u = p["user"]
        parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", u) + re.split(r"[^A-Za-z]+", u)
        words.update(w.lower() for w in parts if len(w) >= 3)
        words.update(w.lower() for w in _sign_off_names(p["text"]))
    in_title = {w.lower() for w in _WORD.findall(title or "")}
    return {w for w in words if w not in NOT_NAMES and w not in in_title}


_LETTER_REF = re.compile(r"\b(?:participant|user|member|poster|person)s?\s+[A-Z]\b|\b[A-Z](?:\s*,\s*[A-Z])*\s+(?:and|or)\s+[A-Z]\b")


def write_problems(d, names):
    out = []
    q, pos, oc = d["question"], d["positions"], d["outcome"]
    if len(q.split()) > 28 or not q.endswith("?"):
        out.append("question")
    if d["discussion"] and not 2 <= len(pos) <= 4:
        out.append("positions count")
    if any(len(p.split()) > 45 for p in pos) or len(oc.split()) > 70:
        out.append("length")
    text = " ".join([q, *pos, oc])
    if {w.lower() for w in re.findall(r"\b[A-ZÀ-Ý][\w'’-]*", text)} & names:
        out.append("names")
    if _LETTER_REF.search(text):
        out.append("letters")
    return out


def build_write_input(row):
    posts = split_raw(row["thread_raw"] or "")
    st = thread_stats(posts)
    labels, scrub = anonymise(posts)
    first = scrub(_CODE_BLOCK.sub("[code]", clean(_designature(row["first_post_en"] or ""), 2500)))
    parts, total = [], 0
    replies = posts[1:]
    budget = WRITE_THREAD_CHARS
    keep = _spread(len(replies), max(1, budget // 280))
    prev = -1
    for i in keep:
        p = replies[i]
        t = _QUOTE.sub("", clean(_designature(p["text"]), 2000))
        t = _CODE_BLOCK.sub("[code]", t)
        t = scrub(re.sub(r"\s+", " ", re.sub(r"-{5,}", " ", t)).strip())
        if not re.search(r"\w", t):
            continue
        if len(t) > WRITE_REPLY_CHARS:
            t = t[:WRITE_REPLY_CHARS].rsplit(" ", 1)[0] + " …"
        line = (f"[… {i - prev - 1} replies skipped]\n" if i > prev + 1 else "") + f"- {labels[p['user']]}: {t}"
        if total + len(line) > budget:
            break
        parts.append(line)
        total += len(line)
        prev = i
    user = (f"Thread title: {row['title_en'] or row['title']}\n"
            f"{st['posts']} posts by {st['participants']} participants over {st['days']} days.\n\n"
            f"Opening post by A (English):\n{first}\n\n"
            f"Replies (original language, shortened):\n" + ("\n".join(parts) or "(none)"))
    if row["summary_paragraph"]:
        user += f"\n\nSummary of the thread: {scrub(row['summary_paragraph'])}"
    return user + "\n\n" + WRITE_TASK, forbidden_names(posts, row["title_en"] or row["title"])


def write_one(llm, row):
    user, names = build_write_input(row)
    best, last_err = None, None
    for attempt in range(4):
        out = llm.chat(WRITE_SYSTEM, user, schema=WRITE_SCHEMA, max_tokens=900, temperature=0.2 + 0.25 * attempt)
        try:
            d = json.loads(out)
            d = {"question": re.sub(r"\s+", " ", d["question"]).strip(),
                 "positions": [re.sub(r"\s+", " ", p).strip() for p in d["positions"] if p.strip()],
                 "outcome": re.sub(r"\s+", " ", d["outcome"]).strip(),
                 "discussion": bool(d["discussion"])}
        except (json.JSONDecodeError, KeyError, AttributeError, TypeError) as e:
            last_err = e
            continue
        d["question"] = re.sub(r"[.!:]+$", "", d["question"])
        if not d["question"].endswith("?"):
            d["question"] += "?"
        d["problems"] = write_problems(d, names)
        if not d["problems"]:
            return d
        if best is None or ("names" in best["problems"]) > ("names" in d["problems"]) or (
                ("names" in best["problems"]) == ("names" in d["problems"]) and len(d["problems"]) < len(best["problems"])):
            best = d
    if best is None:
        raise last_err or ValueError("no output")
    return best


def write(con, base_url, model, workers=4, limit=None, redo=False, budget_minutes=None, log=print):
    """Write-ups for the selected threads (latest current-version Voices classification)."""
    llm = LLM(base_url, model)
    model_id = llm.model_id()
    rows = con.execute(
        """SELECT t.id, t.title, t.title_en, t.first_post_en, t.thread_raw, t.summary_paragraph, c.answers,
                  c.created_at AS rated_at, v.created_at AS written_at, v.prompt_version AS written_version,
                  v.question AS written
           FROM classifications c JOIN topics t ON t.id = c.topic_id
           LEFT JOIN voices_entries v ON v.topic_id = t.id
           WHERE c.prompt_version = ? ORDER BY t.id DESC""", (PROMPT_VERSION,)).fetchall()
    best = {}
    for r in rows:
        if r["id"] not in best or r["rated_at"] > best[r["id"]]["rated_at"]:
            best[r["id"]] = r
    todo = [r for r in best.values() if selected(json.loads(r["answers"])) and (
        redo or not r["written"] or r["written_version"] != WRITE_VERSION or r["written_at"] < r["rated_at"])]
    todo.sort(key=lambda r: -r["id"])
    if limit:
        todo = todo[:limit]
    log(f"[voices-write] {len(todo)} threads with {model_id} ({WRITE_VERSION})")

    def save(row, d):
        con.execute(
            """INSERT OR REPLACE INTO voices_entries (topic_id, question, positions, outcome, discussion, problems,
                                                      model, prompt_version, created_at, error)
               VALUES (?,?,?,?,?,?,?,?,?,NULL)""",
            (row["id"], d["question"], json.dumps(d["positions"], ensure_ascii=False), d["outcome"],
             int(d["discussion"]), json.dumps(d["problems"]), model_id, WRITE_VERSION, now()))

    def fail(row, err):
        con.execute("""INSERT INTO voices_entries (topic_id, error, model, prompt_version, created_at)
                       VALUES (?,?,?,?,?) ON CONFLICT(topic_id) DO UPDATE SET error=excluded.error""",
                    (row["id"], err, model_id, WRITE_VERSION, now()))

    _pool(con, todo, lambda row: write_one(llm, row), save, workers, "voices-write", log, budget_minutes, fail)


def write_stats(con, log=print):
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='voices_entries'").fetchone():
        return
    r = con.execute("""SELECT COUNT(question) ok, SUM(discussion = 0) nodisc,
                              SUM(problems LIKE '%names%') names, SUM(problems IS NOT NULL AND problems != '[]') flawed,
                              COUNT(error) err FROM voices_entries""").fetchone()
    log(f"voices-write: {r['ok']} write-ups ({r['nodisc']} not a discussion, {r['flawed']} with flaws, "
        f"{r['names']} naming someone: never published), {r['err']} errors")
