"""Polite, resumable scraper for discuss.4d.com (Discourse).

The forum rejects generic crawler user agents; its robots.txt allow-lists the
"4d" agent, so we identify as such and honour its Disallow rules (we only use
/site.json, /c/... listings and /raw/...).
"""

import json
import re
import time
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone

import httpx

from . import db

BASE = "https://discuss.4d.com"
USER_AGENT = "4d feature-request-miner/0.1 (keisuke.miyako@4d.com)"
RAW_PAGE_SIZE = 100
STALL_TIMEOUT = 900

_POST_HEADER = re.compile(r"^(?P<user>.+?) \| (?P<date>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC) \| #(?P<n>\d+)$", re.M)
_SEPARATOR = re.compile(r"\n+-{25}\n+")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Client:
    def __init__(self, min_interval=1.0, timeout=60, log=print):
        self.log = log
        self.http = httpx.Client(
            base_url=BASE,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=True,
        )
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next = 0.0

    def _throttle(self):
        with self._lock:
            wait = self._next - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._next = time.monotonic() + self.min_interval

    def get(self, path, params=None, retries=6):
        for attempt in range(retries):
            self._throttle()
            try:
                r = self.http.get(path, params=params)
            except httpx.TransportError as e:
                err = e
                self.log(f"[http] {path}: {e!r}, retry {attempt + 1}")
                time.sleep(min(60, 5 * 2**attempt))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                retry_after = r.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else min(120, 5 * 2**attempt)
                err = httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
                self.log(f"[http] {path}: {r.status_code}, waiting {delay:.0f}s (Retry-After={retry_after})")
                time.sleep(min(delay, 900))
                continue
            r.raise_for_status()
            return r
        raise err


# ---------------------------------------------------------------- listing

def sync_categories(client, con):
    site = client.get("/site.json").json()
    for c in site["categories"]:
        con.execute(
            "INSERT INTO categories(id,slug,name,parent_id,topic_count) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET slug=excluded.slug,name=excluded.name,"
            "parent_id=excluded.parent_id,topic_count=excluded.topic_count",
            (c["id"], c["slug"], c["name"], c.get("parent_category_id"), c.get("topic_count")),
        )
    con.commit()
    return con.execute("SELECT * FROM categories WHERE topic_count > 0 ORDER BY topic_count").fetchall()


def _upsert_topic(con, t):
    """Insert/update listing metadata. Returns True if the topic is new or has new activity."""
    row = con.execute("SELECT bumped_at FROM topics WHERE id=?", (t["id"],)).fetchone()
    changed = row is None or row["bumped_at"] != t.get("bumped_at")
    con.execute(
        """INSERT INTO topics(id,slug,url,title,category_id,tags,created_at,bumped_at,last_posted_at,
               posts_count,views,like_count,vote_count,closed,archived,has_accepted_answer,listed_at)
           VALUES(:id,:slug,:url,:title,:category_id,:tags,:created_at,:bumped_at,:last_posted_at,
               :posts_count,:views,:like_count,:vote_count,:closed,:archived,:has_accepted_answer,:listed_at)
           ON CONFLICT(id) DO UPDATE SET slug=excluded.slug,url=excluded.url,title=excluded.title,
               category_id=excluded.category_id,tags=excluded.tags,bumped_at=excluded.bumped_at,
               last_posted_at=excluded.last_posted_at,posts_count=excluded.posts_count,views=excluded.views,
               like_count=excluded.like_count,vote_count=excluded.vote_count,closed=excluded.closed,
               archived=excluded.archived,has_accepted_answer=excluded.has_accepted_answer,
               listed_at=excluded.listed_at""",
        {
            "id": t["id"],
            "slug": t["slug"],
            "url": f"{BASE}/t/{t['slug']}/{t['id']}",
            "title": t["title"],
            "category_id": t["category_id"],
            "tags": json.dumps([x["name"] if isinstance(x, dict) else x for x in t.get("tags", [])]),
            "created_at": t.get("created_at"),
            "bumped_at": t.get("bumped_at"),
            "last_posted_at": t.get("last_posted_at"),
            "posts_count": t.get("posts_count"),
            "views": t.get("views"),
            "like_count": t.get("like_count"),
            "vote_count": t.get("vote_count"),
            "closed": int(bool(t.get("closed"))),
            "archived": int(bool(t.get("archived"))),
            "has_accepted_answer": int(bool(t.get("has_accepted_answer"))),
            "listed_at": now(),
        },
    )
    return changed


def list_topics(client, con, full=False, log=print):
    """Walk every category listing (ordered by latest activity).

    full=True walks every page (resumable via state); otherwise stops at the
    first page with no new or bumped topics.
    """
    categories = sync_categories(client, con)
    for cat in categories:
        key = f"list_page:{cat['id']}"
        page = db.get_state(con, key, 0) if full else 0
        if page == -1:
            continue
        while True:
            data = client.get(f"/c/{cat['slug']}/{cat['id']}/l/latest.json", params={"page": page}).json()
            topics = [t for t in data["topic_list"]["topics"] if t.get("archetype", "regular") == "regular"]
            changed = sum(_upsert_topic(con, t) for t in topics)
            con.commit()
            log(f"[list] {cat['slug']} page {page}: {len(topics)} topics, {changed} new/changed")
            if not data["topic_list"]["topics"] or not data["topic_list"].get("more_topics_url"):
                break
            if not full and changed == 0 and not any(t.get("pinned") for t in topics):
                break
            page += 1
            if full:
                db.set_state(con, key, page)
        if full:
            db.set_state(con, key, -1)
    if full:
        for cat in categories:
            db.set_state(con, f"list_page:{cat['id']}", 0)


# ---------------------------------------------------------------- raw threads

def split_raw(raw):
    """Split /raw/<topic> output into [{user, date, n, text}]."""
    posts = []
    for chunk in _SEPARATOR.split(raw.strip()):
        m = _POST_HEADER.match(chunk.strip())
        if not m:
            if posts:
                posts[-1]["text"] += "\n" + chunk
            continue
        posts.append({
            "user": m["user"],
            "date": m["date"],
            "n": int(m["n"]),
            "text": chunk.strip()[m.end():].strip(),
        })
    return posts


def fetch_thread(client, topic_id, posts_count):
    pages = max(1, -(-(posts_count or 1) // RAW_PAGE_SIZE))
    parts = []
    for p in range(1, pages + 1):
        r = client.get(f"/raw/{topic_id}", params={"page": p} if p > 1 else None)
        parts.append(r.text.strip())
    return "\n\n-------------------------\n\n".join(parts)


def store_raw(con, topic_id, bumped_at, raw):
    """Save a fetched thread. Returns True if the text differs from what was stored."""
    posts = split_raw(raw)
    first = posts[0] if posts else None
    stamp = now()
    # vendored copy: raw_changed_at only moves when the text really changed (SET sees the old row values)
    cur = con.execute(
        """UPDATE topics SET raw_changed_at = CASE WHEN thread_raw IS ? THEN raw_changed_at ELSE ? END,
               thread_raw=?, first_post_raw=?, op_username=?, raw_bumped_at=?, raw_fetched_at=?, fetch_error=NULL
           WHERE id=? RETURNING raw_changed_at""",
        (raw, stamp, raw, first["text"] if first else raw, first["user"] if first else None,
         bumped_at, stamp, topic_id),
    )
    row = cur.fetchone()
    return bool(row and row[0] == stamp)


def fetch_raw(client, con, workers=2, limit=None, log=print):
    rows = con.execute(
        """SELECT id, posts_count, bumped_at FROM topics
           WHERE thread_raw IS NULL OR raw_bumped_at IS NOT bumped_at
           ORDER BY (category_id = ?) DESC, id DESC""",
        (db.FEATURE_REQUEST_CATEGORY,),
    ).fetchall()
    if limit:
        rows = rows[:limit]
    log(f"[raw] {len(rows)} topics to fetch")
    done = 0
    started = time.monotonic()

    def work(row):
        try:
            return row, fetch_thread(client, row["id"], row["posts_count"]), None
        except Exception as e:  # keep going; error is recorded and retried next run
            return row, None, repr(e)

    pool = ThreadPoolExecutor(workers)
    pending = {pool.submit(work, r) for r in rows}
    try:
        while pending:
            finished, pending = wait(pending, timeout=STALL_TIMEOUT, return_when=FIRST_COMPLETED)
            if not finished:
                raise RuntimeError(f"no progress for {STALL_TIMEOUT}s, aborting (rerun to resume)")
            for fut in finished:
                row, raw, err = fut.result()
                if err:
                    con.execute("UPDATE topics SET fetch_error=? WHERE id=?", (err, row["id"]))
                else:
                    store_raw(con, row["id"], row["bumped_at"], raw)
                con.commit()
                done += 1
                if done % 25 == 0 or done == len(rows):
                    rate = done / (time.monotonic() - started)
                    eta = (len(rows) - done) / rate / 60 if rate else 0
                    log(f"[raw] {done}/{len(rows)}  {rate:.2f}/s  eta {eta:.0f} min" + (f"  last error: {err}" if err else ""))
    finally:
        # never keep crawling in the background if the main loop dies (e.g. database locked)
        pool.shutdown(wait=False, cancel_futures=True)
