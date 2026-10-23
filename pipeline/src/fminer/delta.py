"""Incremental (delta) update — addition in the vendored copy used by GitHub Actions.

* ``latest``: walk the site-wide ``/latest.json`` (ordered by last bump) until a page brings no new or bumped
  topic. Cheaper than walking every category listing; new topics and new replies both bump a topic.
* ``fetch``: the existing ``scrape.fetch_raw`` (topics whose ``bumped_at`` moved since their text was fetched).
* ``sweep``: edits do *not* bump a topic, so re-fetch ``/raw/{id}`` for (a) topics active in the last N days
  and (b) a rolling window of the topics whose text was checked longest ago, and compare the text.
  Changed topics get a new ``raw_changed_at`` so ``enrich --stale`` / ``classify --stale`` redo them.
"""

import time
import urllib.robotparser

from . import db, scrape
from .scrape import BASE, USER_AGENT, now


def check_robots(client, paths, log=print):
    """Refuse to run if robots.txt disallows any path we are about to use for our user agent."""
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(client.get("/robots.txt").text.splitlines())
    blocked = [p for p in paths if not rp.can_fetch(USER_AGENT, BASE + p)]
    if blocked:
        raise SystemExit(f"robots.txt disallows {blocked} for {USER_AGENT!r}; aborting")
    log(f"[robots] allowed: {', '.join(paths)}")


def latest(client, con, max_pages=200, log=print):
    scrape.sync_categories(client, con)
    total = 0
    for page in range(max_pages):
        data = client.get("/latest.json", params={"page": page}).json()
        listed = data["topic_list"]["topics"]
        topics = [t for t in listed if t.get("archetype", "regular") == "regular"]
        changed = [t for t in topics if scrape._upsert_topic(con, t)]
        con.commit()
        fresh = [t for t in changed if not t.get("pinned")]
        total += len(changed)
        log(f"[latest] page {page}: {len(topics)} topics, {len(changed)} new/bumped")
        if not listed or not data["topic_list"].get("more_topics_url") or not fresh:
            break
    return total


def sweep(client, con, recent_days=45, window=300, since=None, log=print):
    """Re-fetch recently active topics and a rolling window of old ones; skip topics fetched after `since`."""
    started = since or now()
    recent = con.execute(
        """SELECT id, bumped_at FROM topics
           WHERE thread_raw IS NOT NULL AND bumped_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)
             AND raw_fetched_at < ?
           ORDER BY bumped_at DESC""",
        (f"-{int(recent_days)} days", started),
    ).fetchall()
    seen = {r["id"] for r in recent}
    rolling = [r for r in con.execute(
        """SELECT id, bumped_at FROM topics WHERE thread_raw IS NOT NULL AND raw_fetched_at < ?
           ORDER BY raw_fetched_at ASC, id LIMIT ?""",
        (started, window + len(seen)),
    ) if r["id"] not in seen][:window]
    rows = recent + rolling
    log(f"[sweep] re-checking {len(recent)} recent + {len(rolling)} rolling-window topics for edits")
    changed, t0 = 0, time.monotonic()
    for i, r in enumerate(rows, 1):
        posts_count = con.execute("SELECT posts_count FROM topics WHERE id=?", (r["id"],)).fetchone()[0]
        try:
            raw = scrape.fetch_thread(client, r["id"], posts_count)
        except Exception as e:  # deleted/private topics etc.: record and keep going
            # also stamp the attempt so dead topics rotate to the back of the rolling window
            con.execute("UPDATE topics SET fetch_error=?, raw_fetched_at=? WHERE id=?", (repr(e), now(), r["id"]))
            con.commit()
            continue
        if scrape.store_raw(con, r["id"], r["bumped_at"], raw):
            changed += 1
        con.commit()
        if i % 50 == 0 or i == len(rows):
            log(f"[sweep] {i}/{len(rows)}  {changed} edited  {i / (time.monotonic() - t0):.2f}/s")
    db.set_state(con, "last_sweep", {"at": started, "checked": len(rows), "edited": changed})
    return changed


def pending(con):
    q = lambda sql, *a: con.execute(sql, a).fetchone()[0]
    return {
        "to_fetch": q("SELECT COUNT(*) FROM topics WHERE thread_raw IS NULL OR raw_bumped_at IS NOT bumped_at"),
        "to_enrich": q("""SELECT COUNT(*) FROM topics WHERE first_post_raw IS NOT NULL AND
                          (enriched_at IS NULL OR enriched_at < COALESCE(raw_changed_at, ''))"""),
    }


def run(client, con, full_list=False, sweep_recent_days=45, sweep_window=300, log=print):
    check_robots(client, ["/site.json", "/latest.json", "/c/feature-request/27/l/latest.json", "/raw/1"])
    started = now()
    if full_list:
        scrape.list_topics(client, con, full=True, log=log)
    else:
        latest(client, con, log=log)
    scrape.fetch_raw(client, con, workers=1, log=log)
    edited = sweep(client, con, sweep_recent_days, sweep_window, since=started, log=log) if sweep_window or sweep_recent_days else 0
    db.set_state(con, "last_delta", {"at": started, "edited": edited, **pending(con)})
    log(f"[delta] done; edited={edited} pending={pending(con)}")
