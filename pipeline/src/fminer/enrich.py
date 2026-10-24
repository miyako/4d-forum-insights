"""Language detection, translation to English and summaries via an
OpenAI-compatible chat endpoint (llama-server)."""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from . import db
from .scrape import now, split_raw

MAX_POST_CHARS = 8000
MAX_REPLIES_CHARS = 2500

_CODE_BLOCK = re.compile(r"```.*?```|\[code\].*?\[/code\]", re.S)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_QUOTE = re.compile(r"\[quote[^\]]*\].*?\[/quote\]", re.S)
_WORD = re.compile(r"[a-zA-ZÀ-ÿ']+")

_STOPWORDS = {
    "en": "the and is are you to of for with this that have it not can how what when there but my be".split(),
    "fr": "le la les des est une un et pour pas dans que qui avec sur je vous il ce mais du au".split(),
    "de": "der die das und ist nicht ich ein eine mit zu von den auf für sie es wie auch dem".split(),
    "es": "el los las que una por para con del como pero está es se lo al mi su".split(),
    "it": "il che di non per una sono della con gli anche come questo ho".split(),
}
_CATEGORY_LANG = {"french-community": "fr", "german-community": "de", "japanese-community": "ja", "spanish-forum": "es"}
LANG_NAMES = {"en": "English", "fr": "French", "de": "German", "ja": "Japanese", "es": "Spanish", "it": "Italian"}


def clean(text, limit=MAX_POST_CHARS):
    text = _IMAGE.sub("[image]", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > limit:
        text = text[:limit] + "\n[…truncated]"
    return text


def detect_lang(text, category_slug=None):
    """Cheap detector for the languages used on this forum (code is ignored)."""
    prose = _CODE_BLOCK.sub(" ", text or "")
    cjk = len(re.findall(r"[\u3040-\u30ff\u4e00-\u9fff]", prose))
    if cjk >= 10 and cjk / max(1, len(prose)) > 0.05:
        return "ja"
    words = [w.lower() for w in _WORD.findall(prose)]
    counts = {lang: sum(w in set(sw) for w in words) for lang, sw in _STOPWORDS.items()}
    best = max(counts, key=counts.get)
    hint = _CATEGORY_LANG.get(category_slug, "en")
    if counts[best] < 3:
        return hint
    if hint in counts and counts[hint] >= 0.8 * counts[best]:
        return hint
    return best


def protect_code(text):
    blocks = []

    def keep(m):
        blocks.append(m.group(0))
        return f"⟦CODE{len(blocks)}⟧"

    return _CODE_BLOCK.sub(keep, text), blocks


def restore_code(text, blocks):
    for i, b in enumerate(blocks, 1):
        if f"⟦CODE{i}⟧" in text:
            text = text.replace(f"⟦CODE{i}⟧", b)
        else:
            text += "\n\n" + b
    return text


class LLM:
    def __init__(self, base_url, model):
        self.http = httpx.Client(base_url=base_url, timeout=600)
        self.model = model

    def chat(self, system, user, schema=None, max_tokens=2048, temperature=0.2):
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}}
        r = self.http.post("/chat/completions", json=body)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    def model_id(self):
        if self.model:
            return self.model
        try:
            return self.http.get("/models").json()["data"][0]["id"]
        except Exception:
            return "unknown"


TRANSLATE_SYSTEM = (
    "You translate posts from the 4D developer forum (4D is a database and application development platform "
    "with its own programming language) into natural, faithful English. Keep 4D command names, class names, "
    "menu items, product names (4D Write Pro, 4D View Pro, 4D Server, ORDA, Qodly…), URLs, markdown formatting "
    "and placeholders like ⟦CODE1⟧ exactly as they are. Do not add commentary. Output the translated title on "
    "the first line prefixed with 'Title: ', then a blank line, then the translated body."
)

SUMMARY_SYSTEM = (
    "You summarise threads from the 4D developer forum (4D is a database and application development platform). "
    "Write in English, be specific about the 4D feature or component involved, and do not speculate. "
    "summary_line must be one complete, self-contained sentence (no lists, no code, no trailing colon)."
)

# gemma-4-E4B returns an empty summary_paragraph about half the time unless the grammar forbids it
MIN_LINE = 30
MIN_PARAGRAPH = 80

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary_line": {"type": "string", "minLength": MIN_LINE, "maxLength": 400,
                         "description": "one sentence, max 25 words, what the author wants or reports"},
        "summary_paragraph": {"type": "string", "minLength": MIN_PARAGRAPH, "maxLength": 1500,
                              "description": "3-5 sentences: the problem or request, context, and how the thread resolved (if replies are given)"},
    },
    "required": ["summary_line", "summary_paragraph"],
}


def translate(llm, title, body, lang):
    protected, blocks = protect_code(body)
    out = llm.chat(
        TRANSLATE_SYSTEM,
        f"Source language: {LANG_NAMES.get(lang, lang)}\n\nTitle: {title}\n\n{protected}",
        max_tokens=4096,
    )
    m = re.match(r"\s*Title:\s*(.*?)\n(.*)", out, re.S)
    title_en, body_en = (m.group(1).strip(), m.group(2).strip()) if m else (title, out)
    return title_en, restore_code(body_en, blocks)


def summarise(llm, title_en, body_en, replies):
    user = f"Title: {title_en}\n\nOriginal post:\n{_CODE_BLOCK.sub('[code]', body_en)}"
    if replies:
        user += f"\n\nReplies (may be in another language, possibly truncated):\n{replies}"
    best, last_err = None, None
    for attempt in range(4):
        out = llm.chat(SUMMARY_SYSTEM, user, schema=SUMMARY_SCHEMA, max_tokens=1000, temperature=0.2 + 0.25 * attempt)
        try:
            d = json.loads(out)
        except json.JSONDecodeError as e:
            last_err = e
            continue
        d = {k: d[k].strip() for k in ("summary_line", "summary_paragraph")}
        if len(d["summary_line"]) >= MIN_LINE and len(d["summary_paragraph"]) >= MIN_PARAGRAPH:
            return d
        if best is None or len(d["summary_paragraph"]) > len(best["summary_paragraph"]):
            best = d
    if best is None:
        raise last_err
    return best


def replies_excerpt(thread_raw):
    posts = split_raw(thread_raw or "")[1:]
    parts, total = [], 0
    for p in posts:
        t = _QUOTE.sub("", _CODE_BLOCK.sub("[code]", clean(p["text"], 800)))
        t = f"- {p['user']}: {t.strip()}"
        if total + len(t) > MAX_REPLIES_CHARS:
            break
        parts.append(t)
        total += len(t)
    return "\n".join(parts)


def enrich_one(llm, row):
    body = clean(row["first_post_raw"])
    lang = detect_lang(row["title"] + "\n" + body, row["category_slug"])
    if lang == "en":
        title_en, body_en = row["title"], body
    else:
        title_en, body_en = translate(llm, row["title"], body, lang)
    s = summarise(llm, title_en, body_en, replies_excerpt(row["thread_raw"]))
    return {
        "lang": lang,
        "title_en": title_en,
        "first_post_en": body_en,
        "summary_line": s["summary_line"],
        "summary_paragraph": s["summary_paragraph"],
    }


def resummarise(con, llm, model_id, workers=4, limit=None, budget_minutes=None, log=print):
    """Backfill missing/short summaries from the stored English text (no re-translation).
    Classifications that used the old summary in their state are deleted so `classify` redoes them."""
    rows = con.execute(
        """SELECT id, title_en, first_post_en, thread_raw FROM topics
           WHERE enriched_at IS NOT NULL
             AND (length(trim(COALESCE(summary_paragraph, ''))) < ? OR length(trim(COALESCE(summary_line, ''))) < ?)
           ORDER BY (category_id = ?) DESC, id DESC""",
        (MIN_PARAGRAPH, MIN_LINE, db.FEATURE_REQUEST_CATEGORY),
    ).fetchall()
    if limit:
        rows = rows[:limit]
    log(f"[resummarise] {len(rows)} topics with {model_id}")

    def work(row):
        return summarise(llm, row["title_en"], row["first_post_en"], replies_excerpt(row["thread_raw"]))

    def save(row, s):
        con.execute(
            "UPDATE topics SET summary_line=?, summary_paragraph=?, enrich_model=?, enrich_error=NULL WHERE id=?",
            (s["summary_line"], s["summary_paragraph"], model_id, row["id"]),
        )
        con.execute("DELETE FROM classifications WHERE topic_id=? AND model NOT LIKE '%:original'", (row["id"],))

    _run_pool(con, rows, work, save, workers, "resummarise", log, budget_minutes)


def _run_pool(con, rows, work, save, workers, label, log, budget_minutes=None):
    started, done, short = time.monotonic(), 0, 0
    # vendored copy: --budget-minutes stops starting new topics after the deadline (time-boxed CI runs)
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
            if err:
                con.execute("UPDATE topics SET enrich_error=? WHERE id=?", (err, row["id"]))
            else:
                save(row, out)
                short += len(out["summary_paragraph"]) < MIN_PARAGRAPH
            con.commit()
            done += 1
            if done % 10 == 0 or done == len(rows):
                rate = done / (time.monotonic() - started)
                log(f"[{label}] {done}/{len(rows)}  {rate*60:.1f}/min  eta {(len(rows)-done)/rate/3600:.1f} h"
                    f"  short_paragraphs={short}" + (f"  last error: {err}" if err else ""))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if done < len(rows):
        log(f"[{label}] time budget reached: {len(rows) - done} topics left for the next run")
    db.rebuild_fts(con)


def run(con, base_url, model, workers=4, limit=None, redo=False, stale=False, budget_minutes=None,
        resummarise_empty=False, log=print):
    llm = LLM(base_url, model)
    model_id = llm.model_id()
    if resummarise_empty:
        return resummarise(con, llm, model_id, workers, limit, budget_minutes, log)
    # vendored copy: --stale also re-enriches topics whose thread text changed after enrichment
    todo = "" if redo else (
        "AND (t.enriched_at IS NULL OR t.enriched_at < COALESCE(t.raw_changed_at, ''))" if stale
        else "AND t.enriched_at IS NULL")
    rows = con.execute(
        f"""SELECT t.id, t.title, t.first_post_raw, t.thread_raw, c.slug AS category_slug
            FROM topics t LEFT JOIN categories c ON c.id=t.category_id
            WHERE t.first_post_raw IS NOT NULL {todo}
            ORDER BY (t.category_id = ?) DESC, t.id DESC""",
        (db.FEATURE_REQUEST_CATEGORY,),
    ).fetchall()
    if limit:
        rows = rows[:limit]
    log(f"[enrich] {len(rows)} topics with {model_id}")

    def save(row, out):
        con.execute(
            """UPDATE topics SET lang=:lang, title_en=:title_en, first_post_en=:first_post_en,
                   summary_line=:summary_line, summary_paragraph=:summary_paragraph,
                   enrich_model=:m, enriched_at=:t, enrich_error=NULL WHERE id=:id""",
            {**out, "m": model_id, "t": now(), "id": row["id"]},
        )
        con.execute("DELETE FROM classifications WHERE topic_id=? AND model NOT LIKE '%:original'", (row["id"],))

    _run_pool(con, rows, lambda row: enrich_one(llm, row), save, workers, "enrich", log, budget_minutes)
