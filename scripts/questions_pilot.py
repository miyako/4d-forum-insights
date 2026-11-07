#!/usr/bin/env python3
"""Snapshot and preview the private Questions pilot; no publication or localization."""

import argparse
from collections import Counter
import html
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline/src"))
from fminer import questions


COVERAGE_LABELS = {
    "none": "No useful answer", "mostly_unanswered": "Mostly unanswered", "partial": "Partially answered",
    "mostly_answered": "Mostly answered", "full": "Fully answered",
}
IMPORTANCE_LABELS = {
    "critical": "Critical", "significant": "Significant", "minor": "Minor", "unknown": "Not enough evidence",
}
COMPLEXITY_LABELS = {
    "complex": "Complex", "moderate": "Moderate", "straightforward": "Straightforward",
    "unknown": "Not enough evidence",
}


def decision_coverage(row):
    answers = row["answers"]
    if not answers:
        return "pending"
    if answers["has_question"]["noul"] < 0.5:
        return "not_a_question"
    return row["effective_coverage"]


def confirmation_state(row):
    if row["entry"]:
        return "yes" if row["entry"]["poster_accepts_solution"] else "no"
    return "pending"


def probability_bars(answer, labels=COVERAGE_LABELS):
    escape = html.escape
    lines = []
    for key, label in labels.items():
        p = answer["probabilities"][key]
        selected = key == answer["choice"]
        lines.append(
            f'<div class="probability{" selected" if selected else ""}">'
            f'<span>{escape(label)}{" (chosen)" if selected else ""}</span>'
            f'<meter min="0" max="1" value="{p}" aria-label="{escape(label)} probability"></meter>'
            f'<span>{p:.0%}</span></div>')
    return '<div class="probabilities">' + "".join(lines) + "</div>"


def angle_table(rows, angle, angle_labels, title):
    questions_only = [r for r in rows if r["answers"] and r["answers"]["has_question"]["noul"] >= 0.5]
    cells = Counter((r["effective_coverage"], r["answers"][angle]["choice"]) for r in questions_only)
    labels = list(COVERAGE_LABELS)
    header = '<tr><th scope="col">Coverage used</th>' + "".join(
        f'<th scope="col">{html.escape(label)}</th>' for label in angle_labels.values()) + "</tr>"
    body = ""
    for a in labels:
        body += f'<tr><th scope="row">{html.escape(COVERAGE_LABELS[a])}</th>'
        for b in angle_labels:
            count = cells.get((a, b), 0)
            body += f'<td class="{"populated" if count else "empty"}">{count if count else "—"}</td>'
        body += "</tr>"
    return (
        f'<div class="table-scroll"><table><caption>{html.escape(title)} '
        f'({len(questions_only)} decision-model question threads)</caption>'
        f"<thead>{header}</thead><tbody>{body}</tbody></table></div>")


def snapshot(source, target):
    if target.exists():
        print(f"Reusing pilot DB: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as original:
        with sqlite3.connect(target) as copy:
            original.backup(copy)
    print(f"Copied source read-only to {target}")


def render(rows):
    escape = html.escape
    cards = []
    for row in rows:
        entry = row["entry"]
        answers = row["answers"]
        title = entry["question"] if entry and entry["has_question"] else row["title"]
        coverage = decision_coverage(row)
        confirmed = confirmation_state(row)
        review = bool(row["disagreements"] or row["writer_acceptance_conflict"])
        importance = answers["impact"]["choice"] if answers else "pending"
        complexity = answers["technicality"]["choice"] if answers else "pending"
        fields = ""
        if entry:
            for key, label in (
                ("answer_summary", "What the replies establish"), ("remaining_gap", "Still open"),
                ("version_scope", "Version scope"), ("clarification", "Needs clarification"),
            ):
                if entry[key]:
                    fields += f"<p><strong>{label}:</strong> {escape(entry[key])}</p>"
            fields += "<p>Evidence: " + ", ".join(
                f'<a href="https://discuss.4d.com/t/{row["id"]}/{n}">post {n}</a>'
                for n in entry["evidence_posts"]) + "</p>"
        if row["errors"]:
            fields += '<p class="warning">Errors: ' + escape("; ".join(row["errors"])) + "</p>"
        decision = '<p>Decision results pending.</p>'
        if answers:
            decision = (
                f'<p><strong>Answer coverage:</strong> {escape(COVERAGE_LABELS[answers["coverage"]["choice"]])}</p>'
                + probability_bars(answers["coverage"])
                + ('<p class="warning">The writer flags acceptance, but this is an unvalidated diagnostic. '
                   'It does not change the decision-model coverage.</p>'
                   if row["writer_acceptance_conflict"] else "")
                + f'<p class="meta">Question likelihood: {answers["has_question"]["noul"]:.0%}. '
                'These are model probabilities, not calibrated certainty.</p>'
                + f'<h4>Importance (impact): {escape(IMPORTANCE_LABELS[importance])}</h4>'
                + probability_bars(answers["impact"], IMPORTANCE_LABELS)
                + f'<h4>Complexity: {escape(COMPLEXITY_LABELS[complexity])}</h4>'
                + probability_bars(answers["technicality"], COMPLEXITY_LABELS)
                + '<p><strong>Domains:</strong> '
                + escape(", ".join(row["domains"]) or "Uncertain") + '</p>')
        writer = '<p>Writing results pending.</p>'
        if entry:
            writer = (
                f'<p><strong>Question present:</strong> {"Yes" if entry["has_question"] else "No"}</p>'
                f'<p><strong>Diagnostic acceptance claim:</strong> <span class="confirmation {confirmed}">'
                f'{"Yes" if confirmed == "yes" else "No — not established"}</span></p>'
                '<p class="meta">Unvalidated writer check for a positive acceptance reply from the original poster. '
                'A solved marker alone does not establish that the poster wrote such a reply.</p>'
                + ('<p><strong>Authoritative status:</strong> the forum marks this thread solved. '
                   'It has the same significance whether set by the poster or a moderator.</p>'
                   if row["accepted"] else "")
                + (f'<p><strong>Acceptance evidence:</strong> {escape(entry["acceptance_evidence"])} '
                   f'(<a href="https://discuss.4d.com/t/{row["id"]}/{entry["acceptance_post"]}">'
                   f'post {entry["acceptance_post"]}</a>)</p>' if entry["poster_accepts_solution"] else "")
                + fields)
        detail = {"decision": answers, "writer": entry}
        cards.append(
            f'<article data-coverage="{coverage}" data-confirmed="{confirmed}" '
            f'data-importance="{importance}" data-complexity="{complexity}" '
            f'data-unanswered="{int(row["unanswered"] is True)}" data-disagreement="{int(review)}">'
            f'<h2>{escape(title)}</h2>'
            f'<p><strong>Forum:</strong> {"Solved" if row["accepted"] else "Not marked solved"} · '
            f'<strong>Thread:</strong> {"Closed" if row["closed"] else "Open" if row["closed"] is False else "Unknown"}</p>'
            f'<p><strong>Replies:</strong> {row["replies"] if row["replies"] is not None else "Unknown"}'
            f'{" — Unanswered (zero replies)" if row["unanswered"] else ""}</p>'
            '<p class="meta">Unreviewed pilot entry</p>'
            + ('<p class="warning">Models differ on whether this is a question.</p>' if row["disagreements"] else "")
            + '<div class="model-columns"><section class="model decision"><h3>Decision model</h3>'
            + f'<p class="model-name">{escape(row["decision_model"] or "Pending")}</p>{decision}</section>'
            + '<section class="model writer"><h3>Writing model</h3>'
            + f'<p class="model-name">{escape(row["entry_model"] or "Pending")}</p>{writer}</section></div>'
            f'<p class="meta">{escape(row["stratum"])} · Last activity: {escape(row["active"] or "unknown")}'
            '</p>'
            f'<p><a href="https://discuss.4d.com/t/{row["id"]}">{escape(row["title"])}</a></p>'
            '<details><summary>Model evidence and probabilities</summary><pre>'
            + escape(json.dumps(detail, indent=2, ensure_ascii=False)) + "</pre></details></article>")
    counts = Counter(decision_coverage(row) for row in rows)
    review_count = sum(bool(r["disagreements"] or r["writer_acceptance_conflict"]) for r in rows)
    options = '<option value="all">All sampled threads</option>' + "".join(
        f'<option value="{key}">{label} ({counts[key]})</option>'
        for key, label in {**COVERAGE_LABELS, "not_a_question": "Not a question", "pending": "Pending"}.items()
        if counts[key])
    importance_options = '<option value="all">All</option>' + "".join(
        f'<option value="{key}">{label}</option>' for key, label in IMPORTANCE_LABELS.items())
    complexity_options = '<option value="all">All</option>' + "".join(
        f'<option value="{key}">{label}</option>' for key, label in COMPLEXITY_LABELS.items())
    angles = angle_table(rows, "impact", IMPORTANCE_LABELS, "Coverage × importance")
    angles += angle_table(rows, "technicality", COMPLEXITY_LABELS, "Coverage × complexity")
    return """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Questions pilot</title>
<style>
:root { color-scheme: light dark; font: 16px/1.55 system-ui, sans-serif; }
body { max-width: 1200px; margin: 2rem auto; padding: 0 1rem; }
article { border: 1px solid #8886; border-radius: 12px; padding: 1.2rem; margin: 1rem 0; }
h2 { font-size: 1.25rem; margin-top: 0; } .meta { opacity: .7; font-size: .9rem; }
.warning { color: light-dark(#963900,#ffbb80); }
input, select { font: inherit; padding: .5rem; max-width: 100%; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: .8rem; }
[hidden] { display: none; } a { color: light-dark(#1655ab,#8ebdff); }
.model-columns { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
.model { min-width: 0; border-radius: 8px; padding: 1rem; border-top: 4px solid; }
.decision { border-color: light-dark(#2464b8,#7bb4ff); background: light-dark(#f0f6ff,#182738); }
.writer { border-color: light-dark(#8650bb,#ce9bff); background: light-dark(#f8f2ff,#2b2038); }
h3 { margin: 0; } .model-name { overflow-wrap: anywhere; font-size: .8rem; opacity: .7; }
.probability { display: grid; grid-template-columns: 10.5rem 1fr 3rem; gap: .5rem; align-items: center; font-size: .85rem; }
.probability meter { width: 100%; accent-color: #2464b8; }
.probability.selected { font-weight: 700; }
.metrics { display: grid; grid-template-columns: auto 1fr; gap: .4rem 1rem; } dd { margin: 0; }
.confirmation { border: 1px solid; border-radius: 5px; padding: .2rem .4rem; }
.confirmation.yes { color: light-dark(#126838,#8be0ad); }
.acceptance-rule { color: light-dark(#126838,#8be0ad); }
.table-scroll { overflow-x: auto; margin: 1.5rem 0; }
table { border-collapse: collapse; width: 100%; font-size: .9rem; }
caption { text-align: left; margin-bottom: .6rem; }
th, td { border: 1px solid #8886; padding: .6rem; } td { text-align: center; }
td.populated { background: light-dark(#dceeff,#203c55); font-weight: bold; }
td.empty { opacity: .4; }
.filters { display: flex; flex-wrap: wrap; gap: 1rem; align-items: center; }
@media (max-width: 760px) { .model-columns { grid-template-columns: 1fr; } }
</style>
<h1>Questions: English pilot</h1>
<p>A small, deliberately balanced sample—not an estimate of how many forum questions are unresolved.
All entries require human review. English source threads only; no original post text is exported.</p>
<p><strong>Solved means solved:</strong> the forum's solved marker is authoritative, whether set by the
poster or a moderator. Model labels do not override it. <strong>Closed is separate</strong> and does not
imply solved. <strong>Unanswered means zero replies</strong>, not a coverage degree.
The decision model assesses explicit acceptance as evidence of full coverage, without changing the forum's solved state.
A version-scoped “no” can be a complete answer. No claim is made about newer versions.</p>
<p><strong>Blue: decision model</strong> — nuanced coverage probabilities and technical ratings.
<strong>Purple: writing model</strong> — generated text and experimental acceptance diagnostics.
The writer does not supply a competing coverage rating. Workarounds contribute to coverage according to
the asker's need; uncertainty stays in the probabilities. Importance is evidenced impact, not complexity.</p>
<p class="warning">Only decision-model results determine question status and coverage. Writer acceptance
claims failed validation and never promote coverage. Existing summaries and translations can be reused
for bulk processing without running the writer again.</p>
""" + angles + """<div class="filters">
<label>Search <input id="search" type="search"></label>
<label>Coverage used <select id="coverage">""" + options + """</select></label>
<label>Importance <select id="importance">""" + importance_options + """</select></label>
<label>Complexity <select id="complexity">""" + complexity_options + """</select></label>
<label>Writer acceptance claim <select id="confirmed"><option value="all">All</option>
<option value="yes">Yes</option><option value="no">No — not established</option>
<option value="pending">Pending</option></select></label>
<label><input id="unanswered" type="checkbox"> Zero replies only</label>
<label><input id="review" type="checkbox"> Review flags (""" + str(review_count) + """)</label>
</div>
<p id="count" aria-live="polite"></p><main>""" + "".join(cards) + """</main>
<script>
const search = document.querySelector('#search'), coverage = document.querySelector('#coverage');
const confirmed = document.querySelector('#confirmed');
const importance = document.querySelector('#importance'), complexity = document.querySelector('#complexity');
const unanswered = document.querySelector('#unanswered');
const review = document.querySelector('#review');
const cards = [...document.querySelectorAll('article')];
function filter() {
  const query = search.value.trim().toLowerCase();
  let shown = 0;
  for (const card of cards) {
    card.hidden = !(coverage.value === 'all' || card.dataset.coverage === coverage.value)
      || !(confirmed.value === 'all' || card.dataset.confirmed === confirmed.value)
      || !(importance.value === 'all' || card.dataset.importance === importance.value)
      || !(complexity.value === 'all' || card.dataset.complexity === complexity.value)
      || (unanswered.checked && card.dataset.unanswered !== '1')
      || !card.textContent.toLowerCase().includes(query)
      || (review.checked && card.dataset.disagreement !== '1');
    if (!card.hidden) shown++;
  }
  document.querySelector('#count').textContent = `${shown} of ${cards.length} threads`;
}
search.addEventListener('input', filter); coverage.addEventListener('change', filter);
confirmed.addEventListener('change', filter);
importance.addEventListener('change', filter); complexity.addEventListener('change', filter);
unanswered.addEventListener('change', filter);
review.addEventListener('change', filter); filter();
</script></html>"""


def report(database, output):
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        rows = questions.report_rows(con)
        settings = questions.db.get_state(con, "questions_pilot")
    if not rows:
        raise ValueError("pilot has no sampled threads; run questions-classify first")
    output.mkdir(parents=True, exist_ok=True)
    payload = {
        "language": "en", "sample": settings, "decision_version": questions.PROMPT_VERSION,
        "writer_version": questions.WRITE_VERSION, "domain_min_p": 0.6,
        "counts": dict(Counter(r["status"] for r in rows)), "threads": rows,
        "coverage_labels": COVERAGE_LABELS,
        "importance_labels": IMPORTANCE_LABELS, "complexity_labels": COMPLEXITY_LABELS,
        "recorded_versions": {
            "decision": sorted({r["decision_version"] for r in rows if r["answers"]}),
            "writer": sorted({r["entry_version"] for r in rows if r["entry"]}),
        },
    }
    (output / "report.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (output / "index.html").write_text(render(rows), encoding="utf-8")
    print(f"Pilot preview: {output / 'index.html'}")
    print(json.dumps(payload["counts"], sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("snapshot")
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--db", type=Path, default=Path("build/questions.db"))
    p = sub.add_parser("report")
    p.add_argument("--db", type=Path, default=Path("build/questions.db"))
    p.add_argument("--out", type=Path, default=Path("build/questions-pilot"))
    args = parser.parse_args()
    if args.command == "snapshot":
        snapshot(args.source, args.db)
    else:
        report(args.db, args.out)


if __name__ == "__main__":
    main()
