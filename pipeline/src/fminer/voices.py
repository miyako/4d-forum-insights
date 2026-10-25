"""Forum Voices: threads where the community weighed in. Not part of upstream 4d-feature-miner.

  voices-classify  decision model (/v1/systemone) rates candidate threads (many participants, several of them
                   posting repeatedly) on five questions:
                   exchange help / views / news / social (Voices shows "views" and "news")
                   debate   how far apart the participants' views are (score, 5 levels)
                   settled  consensus / decided by 4D / unresolved / nothing to settle
                   subject  technical / product direction / business / community
                   heated   the discussion turns personal or emotional (internal: used to rank, never shown)
                   Stored in `classifications` under prompt_version "voices-…" (kept out of the main runs).

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

from .classify import MAX_STATE_CHARS, Decider
from .enrich import _CODE_BLOCK, _QUOTE, clean
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
