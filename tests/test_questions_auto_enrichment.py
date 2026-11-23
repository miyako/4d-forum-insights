from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline/src"))
sys.path.insert(0, str(ROOT / "scripts"))
import questions_auto_update as auto
import questions_prepare_public_update as prepare


class AutomaticEnrichmentTests(unittest.TestCase):
    def test_enrichment_checks_and_returns_model_output(self):
        display = {"title": "Database issue", "body": "The database cannot start."}
        output = {"title": "Database startup", "body": "The database cannot start."}

        def complete(system, data, schema, model, validate):
            self.assertEqual(data, display)
            return validate(output), {"model_calls": 1}

        with (patch.object(prepare.questions_corpus, "split_raw", return_value=[]),
              patch.object(prepare, "complete", side_effect=complete)):
            self.assertEqual(prepare.enrich_display(display, {"thread_raw": ""}, "model"), output)

    def test_known_sensitive_input_is_not_sent(self):
        display = {"title": "Database issue", "body": "Contact example@example.com."}
        with (patch.object(prepare.questions_corpus, "split_raw", return_value=[]),
              patch.object(prepare, "complete") as complete):
            with self.assertRaises(prepare.questions_privacy.ProposalError):
                prepare.enrich_display(display, {"thread_raw": ""}, "model")
            complete.assert_not_called()

    def test_sensitive_model_output_is_rejected(self):
        def complete(system, data, schema, model, validate):
            return validate({"title": "Issue", "body": "Contact example@example.com."}), {}

        with (patch.object(prepare.questions_corpus, "split_raw", return_value=[]),
              patch.object(prepare, "complete", side_effect=complete)):
            with self.assertRaises(prepare.questions_privacy.ProposalError):
                prepare.enrich_display({"title": "Issue", "body": "A problem."},
                                       {"thread_raw": ""}, "model")

    def test_known_forum_identity_is_redacted_without_holding_topic(self):
        display = {"title": "Database issue", "body": "Alice reports the database cannot start."}
        posts = [{"user": "Alice", "text": "The database cannot start."}]

        def complete(system, data, schema, model, validate):
            self.assertNotIn("Alice", data["body"])
            return validate({"title": "Database issue", "body": "The database cannot start."}), {"model_calls": 1}

        with (patch.object(prepare.questions_corpus, "split_raw", return_value=posts),
              patch.object(prepare, "complete", side_effect=complete)):
            self.assertEqual(prepare.enrich_display(display, {"thread_raw": "source"}, "model")["body"],
                             "The database cannot start.")

    def test_inline_code_is_removed_before_openai(self):
        topic = {"id": 1, "title": "Question", "title_en": None, "lang": "en"}
        display, _ = auto.display_for(topic, "Use `example()` here.", None, {})
        self.assertNotIn("example()", display["body"])

    def test_unmarked_source_code_is_not_sent(self):
        display = {"title": "Issue", "body": "The expression QUERY([Items]; $value) fails."}
        with (patch.object(prepare.questions_corpus, "split_raw", return_value=[]),
              patch.object(prepare, "complete") as complete):
            with self.assertRaises(prepare.questions_privacy.ProposalError):
                prepare.enrich_display(display, {"thread_raw": ""}, "model")
            complete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
