"""Offline Questions pilot tests."""

import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline/src"))
from fminer import db, questions

SPEC = importlib.util.spec_from_file_location("questions_preview", ROOT / "scripts/questions_pilot.py")
preview = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preview)


def raw(texts):
    return ("\n\n" + "-" * 25 + "\n\n").join(
        f"{'AlicePrivate' if i == 1 else 'BobPrivate'} | 2026-01-01 12:00:00 UTC | #{i}\n\n{text}"
        for i, text in enumerate(texts, 1))


def answers():
    out = {}
    for key, question in questions.QUESTIONS.items():
        if question["type"] == "noul":
            out[key] = {"noul": 0.8}
        else:
            choices = list(question["criteria"])
            out[key] = {"choice": choices[0], "probabilities": {c: 1 / len(choices) for c in choices}}
    return out


def entry():
    return {
        "question": "Can this setting be changed?",
        "answer_summary": "", "remaining_gap": "No reply explains how to change the setting.",
        "version_scope": "", "clarification": "", "acceptance_evidence": "",
        "has_question": True, "poster_accepts_solution": False, "acceptance_post": 0, "evidence_posts": [1],
    }


class QuestionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "pilot.db"
        self.con = db.connect(self.path)
        self.con.executescript(questions.SCHEMA)

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def topic(self, id=1, lang="en", accepted=0, texts=None, tips=None):
        texts = texts or ["Can this setting be changed?", "No, not in 4D v19."]
        self.con.execute(
            """INSERT INTO topics(id,title,title_en,first_post_en,thread_raw,posts_count,lang,
                                  has_accepted_answer,enriched_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (id, f"Setting {id}", f"Setting {id}", texts[0], raw(texts), len(texts), lang, accepted, "2026-01-01"))
        if tips:
            self.con.execute(
                """INSERT INTO classifications(topic_id,model,prompt_version,answers,created_at)
                   VALUES (?,?,?,?,?)""",
                (id, "tips-model", "tips-v1-test", json.dumps({"qa": {"choice": tips}}), "2026-01-01"))
        self.con.commit()
        return self.con.execute("SELECT * FROM topics WHERE id=?", (id,)).fetchone()

    def test_balanced_english_only_and_fixed_sample(self):
        for n in range(1, 6):
            self.topic(n, texts=["Question?"])
            self.topic(n + 10, accepted=1)
            self.topic(n + 20, tips="open")
            self.topic(n + 30, tips="solved")
            self.topic(n + 40)
            self.topic(n + 50, lang="fr")
        questions.select_sample(self.con, sample=10)
        first = [tuple(r) for r in self.con.execute("SELECT topic_id,stratum FROM questions_pilot ORDER BY topic_id")]
        counts = dict(self.con.execute("SELECT stratum,COUNT(*) FROM questions_pilot GROUP BY stratum"))
        self.assertEqual(counts, dict.fromkeys(("no_replies", "accepted", "tips_open", "tips_solved", "other"), 2))
        self.topic(100)
        questions.select_sample(self.con, sample=10)
        self.assertEqual(first, [tuple(r) for r in self.con.execute(
            "SELECT topic_id,stratum FROM questions_pilot ORDER BY topic_id")])
        with self.assertRaisesRegex(ValueError, "new pilot DB"):
            questions.select_sample(self.con, sample=11)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM questions_pilot p JOIN topics t ON t.id=p.topic_id WHERE t.lang!='en'").fetchone()[0], 0)

    def test_empty_strata_redistribute_and_small_pool(self):
        for n in range(5):
            self.topic(n + 1)
        questions.select_sample(self.con, sample=30)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM questions_pilot").fetchone()[0], 5)

    def test_invalid_or_empty_sample_fails(self):
        with self.assertRaises(ValueError):
            questions.select_sample(self.con, sample=0)
        with self.assertRaisesRegex(ValueError, "no enriched English"):
            questions.select_sample(self.con)

    def test_excerpts_keep_last_reply_anonymise_and_mark_omissions(self):
        row = self.topic(texts=["Can AlicePrivate change this?"] + ["long reply " * 200] * 15
                         + ["BobPrivate says no in v19."])
        state, cited = questions.evidence(row, 6000)
        self.assertIn(1, cited)
        self.assertIn(17, cited)
        self.assertIn("posts omitted", state)
        self.assertIn("truncated", state)
        self.assertNotIn("AlicePrivate", state)
        self.assertNotIn("BobPrivate", state)
        self.assertIn("no in v19", state)
        self.assertLessEqual(len(state), 6000)

    def test_missing_posts_explicit(self):
        row = self.topic(texts=["Question?"])
        self.con.execute("UPDATE topics SET posts_count=5 WHERE id=1")
        row = self.con.execute("SELECT * FROM topics").fetchone()
        self.assertIn("Some posts are unavailable", questions.evidence(row, 6000)[0])

    def test_excerpts_preserve_poster_resolution_endings_at_all_decision_budgets(self):
        row = self.topic(texts=["Why does the request use the wrong content type?", "Check the request headers.",
                               "Technical context. " * 160 + "Now everything is working as expected."])
        source = row["thread_raw"].replace(
            "BobPrivate | 2026-01-01 12:00:00 UTC | #3",
            "AlicePrivate | 2026-01-01 12:00:00 UTC | #3")
        self.con.execute("UPDATE topics SET thread_raw=? WHERE id=1", (source,))
        row = self.con.execute("SELECT * FROM topics WHERE id=1").fetchone()
        for budget in (2100, 3600, 6000, 14000):
            with self.subTest(budget=budget):
                text, cited = questions.evidence(row, budget)
                self.assertIn("Now everything is working as expected.", text)
                self.assertIn(3, cited)
                self.assertIn("(original poster)", text)
                self.assertLessEqual(len(text), budget)

    def test_larger_writer_input_keeps_the_decision_models_core_posts(self):
        row = self.topic(texts=["Main question?"] + ["Long reply. " * 250] * 29)
        small, small_cited = questions.evidence(row, 2100)
        medium, medium_cited = questions.evidence(row, 6000)
        large, large_cited = questions.evidence(row, 14000)
        self.assertTrue(small_cited <= medium_cited <= large_cited)
        for text, budget in ((small, 2100), (medium, 6000), (large, 14000)):
            self.assertLessEqual(len(text), budget)
        self.assertTrue({1, 2, 30} <= small_cited)

    def test_bounded_excerpt_preserves_both_ends_and_marks_only_real_omissions(self):
        text = "Main question begins here. " + "Detail " * 100 + "The solution worked."
        excerpt = questions.bounded_excerpt(text, 120)
        self.assertTrue(excerpt.startswith("Main question begins here."))
        self.assertTrue(excerpt.endswith("The solution worked."))
        self.assertIn("middle truncated", excerpt)
        self.assertEqual(len(excerpt), 120)
        self.assertEqual(questions.bounded_excerpt("Short reply.", 120), "Short reply.")
        with self.assertRaises(ValueError):
            questions.bounded_excerpt(text, 10)

    def test_excerpts_include_early_answer_and_latest_asker_followup(self):
        texts = ["Question?", "Early answer."] + ["Another reply."] * 15
        row = self.topic(texts=texts)
        updated = row["thread_raw"].replace(
            "BobPrivate | 2026-01-01 12:00:00 UTC | #12",
            "AlicePrivate | 2026-01-01 12:00:00 UTC | #12")
        self.con.execute("UPDATE topics SET thread_raw=? WHERE id=1", (updated,))
        row = self.con.execute("SELECT * FROM topics").fetchone()
        _, cited = questions.evidence(row, 6000)
        self.assertTrue({1, 2, 12, 17} <= cited)

    def test_no_is_an_answer_but_unconfirmed_is_eligible(self):
        self.assertEqual(questions.status(True, "full"), "full")
        self.assertEqual(questions.status(True, "partial"), "partial")
        self.assertEqual(questions.status(True, "mostly_unanswered"), "mostly_unanswered")
        self.assertEqual(questions.status(True, "mostly_answered"), "mostly_answered")
        self.assertEqual(questions.status(False, "none"), "not_a_question")
        result = entry()
        result.update(remaining_gap="", answer_summary="Not supported in 4D v19.")
        questions.validate_entry(result, {1, 2})

    def test_forum_solved_is_authoritative_and_closed_is_independent(self):
        self.assertEqual(questions.status(True, "partial", solved=True), "solved")
        self.assertEqual(questions.status(False, "none", solved=True), "solved")
        self.prepare_entry()
        self.assertIn("<strong>Thread:</strong> Unknown", preview.render(questions.report_rows(self.con)))
        self.con.execute("UPDATE topics SET closed=1 WHERE id=1")
        changed = self.con.execute("SELECT * FROM topics WHERE id=1").fetchone()
        self.con.execute("UPDATE questions_pilot SET input_hash=?,entry_input_hash=?",
                         (questions.input_hash(changed), questions.input_hash(changed)))
        closed = questions.report_rows(self.con)[0]
        self.assertTrue(closed["closed"])
        self.assertFalse(closed["accepted"])
        self.assertEqual(preview.confirmation_state(closed), "no")
        self.assertIn("<strong>Thread:</strong> Closed", preview.render([closed]))
        self.con.execute("UPDATE topics SET has_accepted_answer=1 WHERE id=1")
        solved = questions.report_rows(self.con)[0]
        self.assertEqual(solved["status"], "solved")
        self.assertEqual(preview.confirmation_state(solved), "pending")
        self.assertIn("<strong>Forum:</strong> Solved", preview.render([solved]))

    def test_new_report_does_not_mix_old_prompt_outputs(self):
        self.prepare_entry()
        self.con.execute("UPDATE questions_pilot SET entry_version='earlier-pilot'")
        self.assertIsNone(questions.report_rows(self.con)[0]["entry"])
        row = questions.report_rows(self.con)[0]
        self.assertIn("Writing results pending", preview.render([row]))
        self.assertIsNotNone(self.con.execute("SELECT entry FROM questions_pilot").fetchone()[0])

    def test_validation_rejects_unsupported_evidence_and_inconsistent_output(self):
        cases = [
            {"evidence_posts": [99]}, {"evidence_posts": [True]}, {"evidence_posts": []},
            {"poster_accepts_solution": "true"}, {"acceptance_post": True},
            {"poster_accepts_solution": True}, {"has_question": "true"},
            {"coverage": "bad"}, {"answer_summary": "word " * 81},
            {"acceptance_evidence": "The poster accepted it."},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                questions.validate_entry({**entry(), **changes}, {1, 2})
        with self.assertRaisesRegex(ValueError, "participant"):
            questions.validate_entry({**entry(), "clarification": "Ask AlicePrivate."}, {1}, {"aliceprivate"})
        with self.assertRaisesRegex(ValueError, "participant"):
            questions.validate_entry({**entry(), "answer_summary": "Participant B offered a suggestion."}, {1})
        with self.assertRaisesRegex(ValueError, "not a question"):
            questions.validate_entry({**entry(), "has_question": False}, {1})
        with self.assertRaisesRegex(ValueError, "original asker"):
            questions.validate_entry({**entry(), "poster_accepts_solution": True, "acceptance_post": 2,
                                     "evidence_posts": [1, 2]}, {1, 2}, author_replies={3})

    def test_positive_acceptance_requires_an_actual_asker_reply(self):
        row = self.topic(texts=["Question?", "A solution.", "That worked."])
        source = row["thread_raw"].replace(
            "BobPrivate | 2026-01-01 12:00:00 UTC | #3",
            "AlicePrivate | 2026-01-01 12:00:00 UTC | #3")
        self.con.execute("UPDATE topics SET thread_raw=? WHERE id=1", (source,))
        row = self.con.execute("SELECT * FROM topics WHERE id=1").fetchone()
        author_replies = questions.author_reply_numbers(row)
        self.assertEqual(author_replies, {3})
        accepted = {**entry(), "poster_accepts_solution": True, "acceptance_post": 3,
                    "acceptance_evidence": "The original asker said the solution worked.",
                    "answer_summary": "The suggested setting resolved the issue.",
                    "remaining_gap": "", "evidence_posts": [2, 3]}
        questions.validate_entry(accepted, {1, 2, 3}, author_replies=author_replies)
        with self.assertRaisesRegex(ValueError, "original asker"):
            questions.validate_entry({**accepted, "acceptance_post": 2}, {1, 2, 3}, author_replies=author_replies)
        with self.assertRaisesRegex(ValueError, "empty remaining_gap"):
            questions.validate_entry({**accepted, "remaining_gap": "Still open."}, {1, 2, 3}, author_replies=author_replies)

    def test_decision_probabilities_are_validated(self):
        questions.validate_answers(answers())
        bad = answers()
        bad["has_question"]["noul"] = 2
        with self.assertRaises(ValueError):
            questions.validate_answers(bad)
        bad = answers()
        bad["coverage"]["probabilities"].pop("full")
        with self.assertRaises(ValueError):
            questions.validate_answers(bad)

    def test_report_decisions_strip_arbitrary_server_fields(self):
        result = answers()
        result["source_text"] = "Private original post"
        result["coverage"]["reasoning"] = "Private original post"
        text = json.dumps(questions.display_answers(result))
        self.assertNotIn("Private original post", text)
        self.assertNotIn("reasoning", text)

    def test_classification_resumes_and_tracks_changed_input(self):
        self.topic()
        with patch.object(questions, "QuestionsDecider") as constructor:
            decider = constructor.return_value
            decider.model_id.return_value = "decision"
            decider.ask_fitting.return_value = {"answers": answers()}
            questions.classify(self.con, "http://unused", sample=1, log=lambda _: None)
            questions.classify(self.con, "http://unused", sample=1, log=lambda _: None)
            self.assertEqual(decider.ask_fitting.call_count, 1)
            self.con.execute("UPDATE topics SET has_accepted_answer=1 WHERE id=1")
            questions.classify(self.con, "http://unused", sample=1, log=lambda _: None)
            self.assertEqual(decider.ask_fitting.call_count, 2)
            self.assertEqual(self.con.execute("SELECT COUNT(*) FROM classifications").fetchone()[0], 0)

    def test_failed_classification_is_explicit_and_retriable(self):
        self.topic()
        with patch.object(questions, "QuestionsDecider") as constructor:
            decider = constructor.return_value
            decider.model_id.return_value = "decision"
            decider.ask_fitting.side_effect = RuntimeError("test failure")
            with self.assertRaisesRegex(RuntimeError, "classification failed"):
                questions.classify(self.con, "http://unused", sample=1, log=lambda _: None)
            self.assertIn("test failure", self.con.execute("SELECT decision_error FROM questions_pilot").fetchone()[0])
            decider.ask_fitting.side_effect = None
            decider.ask_fitting.return_value = {"answers": answers()}
            questions.classify(self.con, "http://unused", sample=1, log=lambda _: None)
            self.assertIsNone(self.con.execute("SELECT decision_error FROM questions_pilot").fetchone()[0])

    def prepare_entry(self, texts=None, accepted=0):
        row = self.topic(texts=texts, accepted=accepted)
        questions.select_sample(self.con, sample=1)
        self.con.execute(
            """UPDATE questions_pilot SET answers=?,input_hash=?,decision_version=?,entry=?,entry_input_hash=?,
                      entry_version=?""",
            (json.dumps(answers()), questions.input_hash(row), questions.PROMPT_VERSION,
             json.dumps(entry()), questions.input_hash(row), questions.WRITE_VERSION))
        self.con.commit()

    def test_reports_flag_question_disagreement_and_never_expose_raw(self):
        self.prepare_entry()
        self.con.execute("UPDATE questions_pilot SET answers=json_set(answers,'$.has_question.noul',0.2)")
        self.con.commit()
        report = questions.report_rows(self.con)
        self.assertEqual(report[0]["disagreements"], ["has_question"])
        self.assertEqual(report[0]["status"], "not_a_question")
        text = json.dumps(report)
        self.assertNotIn("thread_raw", text)
        self.assertNotIn("BobPrivate", text)
        preview.report(self.path, Path(self.temp.name) / "report")
        self.assertTrue((Path(self.temp.name) / "report/index.html").exists())

    def test_zero_replies_is_metadata_not_a_coverage_degree(self):
        self.prepare_entry(texts=["Question?"])
        row = questions.report_rows(self.con)[0]
        self.assertTrue(row["unanswered"])
        self.assertEqual(row["replies"], 0)
        self.assertEqual(row["status"], "unanswered")
        self.assertEqual(row["effective_coverage"], "none")
        self.assertNotIn("unanswered", questions.COVERAGE)
        self.assertNotIn("unclear", questions.COVERAGE)
        self.assertNotIn("workaround", questions.COVERAGE)
        self.assertIn("Unanswered (zero replies)", preview.render([row]))

    def test_writer_acceptance_never_changes_decision_coverage_or_solved(self):
        self.prepare_entry()
        accepted = {**entry(), "poster_accepts_solution": True}
        self.con.execute("UPDATE questions_pilot SET entry=?", (json.dumps(accepted),))
        row = questions.report_rows(self.con)[0]
        self.assertFalse(row["accepted"])
        self.assertTrue(row["poster_accepts_solution"])
        self.assertEqual(row["effective_coverage"], "none")
        self.assertEqual(row["answers"]["coverage"]["choice"], "none")
        self.assertEqual(row["coverage_source"], "decision_model")
        self.assertEqual(row["status"], "none")
        self.assertTrue(row["writer_acceptance_conflict"])
        self.assertEqual(preview.confirmation_state(row), "yes")

    def test_solved_marker_does_not_imply_a_positive_poster_reply_or_model_full(self):
        self.prepare_entry(accepted=1)
        row = questions.report_rows(self.con)[0]
        self.assertEqual(row["status"], "solved")
        self.assertFalse(row["poster_accepts_solution"])
        self.assertEqual(row["effective_coverage"], "none")
        self.assertEqual(preview.confirmation_state(row), "no")

    def test_stale_entries_are_not_reported(self):
        self.prepare_entry()
        self.con.execute("UPDATE topics SET thread_raw=? WHERE id=1", (raw(["Changed question?"]),))
        row = questions.report_rows(self.con)[0]
        self.assertIsNone(row["answers"])
        self.assertIsNone(row["entry"])
        self.assertEqual(row["status"], "pending")

    def test_writer_retries_invalid_output(self):
        from unittest.mock import Mock
        row = self.topic()
        llm = Mock()
        llm.chat.side_effect = [json.dumps({**entry(), "evidence_posts": [99]}), json.dumps(entry())]
        self.assertEqual(questions.write_one(llm, row), entry())
        self.assertEqual(llm.chat.call_count, 2)
        schema = llm.chat.call_args.kwargs["schema"]
        for branch in schema["oneOf"]:
            self.assertEqual(branch["properties"]["acceptance_post"]["enum"], [0])
            self.assertEqual(branch["properties"]["poster_accepts_solution"]["enum"], [False])

    def test_non_question_schema_forces_empty_content_without_overriding_question_detection(self):
        schema = questions.write_schema([3, 5])
        non_question, question = schema["oneOf"]
        self.assertEqual(non_question["properties"]["has_question"]["enum"], [False])
        self.assertEqual(question["properties"]["has_question"]["enum"], [True])
        for key in ("question", "answer_summary", "remaining_gap", "acceptance_evidence"):
            self.assertEqual(non_question["properties"][key]["enum"], [""])
        self.assertEqual(non_question["properties"]["acceptance_post"]["enum"], [0])
        self.assertEqual(question["properties"]["acceptance_post"]["enum"], [0, 3, 5])
        self.assertNotIn("enum", ENTRY := questions.ENTRY_SCHEMA["properties"]["has_question"])
        self.assertEqual(ENTRY["type"], "boolean")

    def test_writer_resumes(self):
        self.prepare_entry()
        self.con.execute("UPDATE questions_pilot SET entry=NULL")
        with patch.object(questions, "LLM") as constructor, patch.object(questions, "write_one", return_value=entry()) as writer:
            constructor.return_value.model_id.return_value = "writer"
            questions.write(self.con, "http://unused", log=lambda _: None)
            questions.write(self.con, "http://unused", log=lambda _: None)
            self.assertEqual(writer.call_count, 1)

    def test_writer_errors_are_explicit_and_stale_prompt_is_hidden(self):
        self.prepare_entry()
        self.con.execute("UPDATE questions_pilot SET entry=NULL")
        with patch.object(questions, "LLM") as constructor, patch.object(
                questions, "write_one", side_effect=ValueError("invalid evidence")):
            constructor.return_value.model_id.return_value = "writer"
            with self.assertRaisesRegex(RuntimeError, "writing failed"):
                questions.write(self.con, "http://unused", log=lambda _: None)
        self.assertEqual(questions.report_rows(self.con)[0]["status"], "none")
        self.assertIn("invalid evidence", questions.report_rows(self.con)[0]["errors"][0])
        self.con.execute("UPDATE questions_pilot SET decision_version='old'")
        self.assertIsNone(questions.report_rows(self.con)[0]["answers"])

    def test_writer_revalidates_previously_stored_entries(self):
        self.prepare_entry()
        invalid = {**entry(), "acceptance_post": 99}
        self.con.execute("UPDATE questions_pilot SET entry=?,entry_model='writer'", (json.dumps(invalid),))
        with patch.object(questions, "LLM") as constructor, patch.object(questions, "write_one", return_value=entry()) as writer:
            constructor.return_value.model_id.return_value = "writer"
            questions.write(self.con, "http://unused", log=lambda _: None)
            self.assertEqual(writer.call_count, 1)

    def test_html_escapes_generated_text(self):
        self.prepare_entry()
        rows = questions.report_rows(self.con)
        rows[0]["entry"]["question"] = "<script>alert(1)</script>"
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", preview.render(rows))
        self.assertNotIn("<script>alert(1)</script>", preview.render(rows))

    def test_comparison_distinguishes_binary_confirmation_from_coverage(self):
        self.prepare_entry()
        rows = questions.report_rows(self.con)
        rows[0]["answers"]["coverage"]["choice"] = "partial"
        rows[0]["effective_coverage"] = "partial"
        page = preview.render(rows)
        self.assertIn('class="model decision"', page)
        self.assertIn('class="model writer"', page)
        self.assertIn('data-coverage="partial"', page)
        self.assertIn('data-confirmed="no"', page)
        self.assertIn("No — not established", page)
        self.assertNotIn("Legacy pilot coverage label", page)
        self.assertEqual(page.count("<meter "), len(questions.COVERAGE) + len(preview.IMPORTANCE_LABELS)
                         + len(preview.COMPLEXITY_LABELS))
        self.assertIn("Importance (impact)", page)
        self.assertIn("Complexity", page)
        self.assertNotIn("coverage", rows[0]["entry"])

    def test_angle_tables_use_decision_question_flags_not_writer_flags(self):
        self.prepare_entry()
        row = questions.report_rows(self.con)[0]
        control = {**row, "entry": {**row["entry"], "has_question": False}}
        pending = {**row, "entry": None, "answers": None, "effective_coverage": None}
        low_question_probability = {**row, "answers": {
            **row["answers"], "has_question": {"noul": 0.2}}}
        table = preview.angle_table([row, control, pending, low_question_probability],
                                     "impact", preview.IMPORTANCE_LABELS, "Importance")
        self.assertIn("(2 decision-model question threads)", table)
        self.assertEqual(preview.decision_coverage(low_question_probability), "not_a_question")
        self.assertIn('data-confirmed="pending"', preview.render([pending]))

    def test_snapshot_does_not_modify_source_or_replace_pilot(self):
        self.topic()
        target = Path(self.temp.name) / "copy.db"
        preview.snapshot(self.path, target)
        with sqlite3.connect(target) as con:
            con.execute("UPDATE topics SET title='pilot change'")
        preview.snapshot(self.path, target)
        self.assertEqual(self.con.execute("SELECT title FROM topics").fetchone()[0], "Setting 1")
        with sqlite3.connect(target) as con:
            self.assertEqual(con.execute("SELECT title FROM topics").fetchone()[0], "pilot change")


if __name__ == "__main__":
    unittest.main()
