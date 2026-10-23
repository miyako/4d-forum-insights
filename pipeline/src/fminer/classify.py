"""Classify topics with a decision model through llama-server's /v1/systemone."""

import hashlib
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from . import db
from .enrich import clean, _CODE_BLOCK
from .scrape import now

MAX_STATE_CHARS = 6000

LEVELS = [
    "most unlikely feature request",
    "unlikely feature request",
    "neutral",
    "likely feature request",
    "most likely feature request",
]

KINDS = {
    "feature_request": "asks 4D to add or change a capability of the product",
    "bug_report": "reports a defect, crash or regression",
    "question": "asks how to do something or for help",
    "announcement": "news, release, event, tip or resource",
    "discussion": "opinion, experience or general discussion",
}

QUESTIONS = {
    "fr": {
        "type": "score",
        "instructions": "Is this post from the forum of 4D (a database and application development platform) "
                        "a feature request for the 4D product?",
        "criteria": LEVELS,
    },
    "kind": {
        "type": "choice",
        "instructions": "What kind of forum post is this?",
        "criteria": KINDS,
    },
    "request": {
        "type": "noul",
        "instructions": "Does the author ask 4D to add a new feature or change the behaviour of the 4D product?",
    },
    "gap": {
        "type": "noul",
        "instructions": "Does the post reveal a capability that 4D currently lacks and that the author needs?",
    },
}

PROMPT_VERSION = "v2-" + hashlib.sha1(json.dumps(QUESTIONS, sort_keys=True).encode()).hexdigest()[:8]


def build_state(row, original=False):
    if original:
        title, body = row["title"], clean(row["first_post_raw"], MAX_STATE_CHARS)
    else:
        title, body = row["title_en"], clean(row["first_post_en"], MAX_STATE_CHARS)
    body = _CODE_BLOCK.sub("[code snippet]", body)
    state = f"Forum post title: {title}\n\nForum post:\n{body}"
    if not original and row["summary_paragraph"]:
        state += f"\n\nSummary of the whole thread: {row['summary_paragraph']}"
    return state


class Decider:
    def __init__(self, base_url, model):
        self.http = httpx.Client(base_url=base_url, timeout=300)
        self.model = model

    def model_id(self):
        if self.model:
            return self.model
        return self.http.get("/v1/models").json()["data"][0]["id"]

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


def run(con, base_url, model, workers=4, limit=None, sample=None, original=False, log=print):
    decider = Decider(base_url, model)
    model_id = decider.model_id() + (":original" if original else "")
    ready = "t.first_post_raw IS NOT NULL" if original else "t.enriched_at IS NOT NULL"
    rows = con.execute(
        f"""SELECT t.* FROM topics t
            WHERE {ready} AND NOT EXISTS (
                SELECT 1 FROM classifications c WHERE c.topic_id=t.id AND c.model=? AND c.prompt_version=?)
            ORDER BY t.id DESC""",
        (model_id, PROMPT_VERSION),
    ).fetchall()
    if sample:
        rng = random.Random(42)
        fr = [r for r in rows if r["category_id"] == db.FEATURE_REQUEST_CATEGORY]
        other = [r for r in rows if r["category_id"] != db.FEATURE_REQUEST_CATEGORY]
        rows = rng.sample(fr, min(len(fr), sample // 2)) + rng.sample(other, min(len(other), sample - sample // 2))
    if limit:
        rows = rows[:limit]
    log(f"[classify] {len(rows)} topics with {model_id} ({PROMPT_VERSION})")
    started, done = time.monotonic(), 0

    def work(row):
        try:
            return row, decider.ask(build_state(row, original)), None
        except Exception as e:
            return row, None, repr(e)

    with ThreadPoolExecutor(workers) as pool:
        for fut in as_completed([pool.submit(work, r) for r in rows]):
            row, res, err = fut.result()
            done += 1
            if err:
                log(f"[classify] topic {row['id']}: {err}")
                continue
            a = res["answers"]
            con.execute(
                """INSERT OR REPLACE INTO classifications
                   (topic_id, model, prompt_version, fr_score, fr_probs, fr_confidence, gap_p, request_p,
                    kind, kind_fr_p, answers, input_tokens, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["id"], model_id, PROMPT_VERSION, a["fr"]["score"], json.dumps(a["fr"]["probabilities"]),
                 a["fr"].get("confidence"), a["gap"]["noul"], a["request"]["noul"],
                 a["kind"]["choice"], a["kind"]["probabilities"]["feature_request"], json.dumps(a),
                 res.get("usage", {}).get("input_tokens"), now()),
            )
            con.commit()
            if done % 50 == 0 or done == len(rows):
                rate = done / (time.monotonic() - started)
                log(f"[classify] {done}/{len(rows)}  {rate:.1f}/s")
    con.commit()


def _auc(pos, neg):
    """Mann-Whitney AUC."""
    if not pos or not neg:
        return float("nan")
    ranked = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg])
    rank_sum, i = 0.0, 0
    while i < len(ranked):
        j = i
        while j < len(ranked) and ranked[j][0] == ranked[i][0]:
            j += 1
        avg = (i + j + 1) / 2
        rank_sum += avg * sum(1 for k in range(i, j) if ranked[k][1])
        i = j
    return (rank_sum - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def calibrate(con, model=None, log=print):
    """The FR category gives reliable positives; the rest are *noisy* negatives
    (that is the point of the project), so the AUC is a lower bound."""
    from .query import BUCKETS, bucket_sql

    runs = con.execute("SELECT model, prompt_version, COUNT(*) n FROM classifications "
                       + ("WHERE model=? " if model else "") + "GROUP BY 1,2", (model,) if model else ()).fetchall()
    for run_ in runs:
        rows = con.execute(
            f"""SELECT t.category_id = ? AS fr, c.fr_score, c.request_p, c.gap_p, c.kind_fr_p, {bucket_sql()} AS bucket
                FROM classifications c JOIN topics t ON t.id=c.topic_id WHERE c.model=? AND c.prompt_version=?""",
            (db.FEATURE_REQUEST_CATEGORY, run_["model"], run_["prompt_version"]),
        ).fetchall()
        log(f"\n== {run_['model']} / {run_['prompt_version']}  (n={run_['n']})")
        for col in ("fr_score", "kind_fr_p", "request_p", "gap_p"):
            pos = [r[col] for r in rows if r["fr"] and r[col] is not None]
            neg = [r[col] for r in rows if not r["fr"] and r[col] is not None]
            log(f"  AUC {col:<9} FR-category vs rest: {_auc(pos, neg):.3f}")
        log(f"  {'bucket':<14}{'FR cat':>8}{'other':>8}")
        for b in reversed(BUCKETS):
            log(f"  {b:<14}{sum(1 for r in rows if r['fr'] and r['bucket']==b):>8}"
                f"{sum(1 for r in rows if not r['fr'] and r['bucket']==b):>8}")
