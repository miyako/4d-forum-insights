"""Synthetic span and local-endpoint guard cases; not a PI accuracy evaluation."""

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline/src"))
from fminer import questions_privacy as privacy


def span(text, value, kind="person"):
    start = text.index(value)
    return {"start": start, "end": start + len(value), "text": value, "kind": kind}


class PrivacyTests(unittest.TestCase):
    def test_exact_unicode_indices_preserve_technical_text(self):
        text = "日本語: AlicePrivate uses ORDA and 4D v20; mail alice@example.invalid."
        result = {"uncertain": False, "spans": [span(text, "AlicePrivate"),
                                               span(text, "alice@example.invalid", "email")]}
        output, _ = privacy.apply_spans(text, result)
        self.assertEqual(output, "日本語: [redacted] uses ORDA and 4D v20; mail [redacted].")

    def test_unique_exact_text_proposals_resolve_unicode_offsets_without_fuzzy_correction(self):
        text = "日本語: Élodie uses ORDA."
        output, spans = privacy.resolve_proposals(text, {
            "uncertain": False, "spans": [{"text": "Élodie", "kind": "person"}]})
        self.assertEqual(output, "日本語: [redacted] uses ORDA.")
        self.assertEqual(spans[0]["start"], text.index("Élodie"))
        for bad_text, target in (("Alice and Alice", "Alice"), ("aaaa", "aa"), ("Élodie uses ORDA", "Elodie"),
                                 ("ａｌｉｃｅ uses ORDA", "alice"), ("ALICE uses ORDA", "Alice")):
            with self.subTest(text=bad_text), self.assertRaises(privacy.ProposalError):
                privacy.resolve_proposals(bad_text, {"uncertain": False, "spans": [{"text": target, "kind": "person"}]})
        with self.assertRaises(privacy.ProposalError):
            privacy.resolve_proposals("Alice Demo", {"uncertain": False, "spans": [
                {"text": "Alice Demo", "kind": "person"}, {"text": "Alice", "kind": "person"}]})

    def test_explicit_original_poster_guard_is_not_limited_to_parsed_posts(self):
        posts = [{"user": "OtherParticipant", "text": "Question?"}]
        self.assertIn("normalized_username", privacy.findings("ｈｉｄｄｅｎｏｐ needs ORDA", posts, "HiddenOP"))
        self.assertIn("normalized_username", privacy.findings("山田太郎はORDAを使う", posts, "山田太郎"))
        self.assertIn("normalized_username", privacy.findings("山田はORDAを使う", posts, "山田"))
        self.assertFalse(privacy.findings("Use ORDA.", posts, "HiddenOP"))

    def test_invalid_spans_fail_closed(self):
        text = "AlicePrivate uses ORDA."
        original = span(text, "AlicePrivate")
        cases = [
            {"uncertain": True, "spans": []},
            {"uncertain": False, "spans": [{**original, "start": True}]},
            {"uncertain": False, "spans": [{**original, "end": 999}]},
            {"uncertain": False, "spans": [{**original, "text": "other"}]},
            {"uncertain": False, "spans": [{**original, "kind": "technical"}]},
            {"uncertain": False, "spans": [original, original]},
            {"uncertain": False, "spans": [], "safe": True},
        ]
        for result in cases:
            with self.subTest(result=result), self.assertRaises(ValueError):
                privacy.apply_spans(text, result)

    def test_nfkc_original_poster_and_existing_identifiers(self):
        posts = [{"user": "AlicePrivate", "text": "Question?"}]
        self.assertIn("normalized_username", privacy.findings("ａｌｉｃｅｐｒｉｖａｔｅ uses ORDA", posts))
        self.assertIn("email", privacy.findings("Contact a@example.invalid", posts))
        self.assertIn("path", privacy.findings("Use /Users/alice/private/db", posts))
        self.assertFalse(privacy.findings("Use ORDA in 4D v20.", posts))

    def test_hosted_proxy_and_redirect_endpoints_rejected(self):
        for endpoint in ("https://api.openai.com/v1", "http://example.invalid/v1",
                         "http://user:password@127.0.0.1/v1", "http://127.0.0.1/v1?redirect=1"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                privacy.local_endpoint(endpoint)

    def test_bad_model_response_retries_bounded_and_never_clears(self):
        response = httpx.Response(200, request=httpx.Request("POST", "http://127.0.0.1/v1"),
                                  json={"choices": [{"finish_reason": "stop",
                                                     "message": {"content": json.dumps(
                                                         {"uncertain": False, "spans": []})}}]})
        with patch.object(privacy.httpx, "Client") as client:
            client.return_value.__enter__.return_value.post.return_value = response
            with self.assertRaisesRegex(ValueError, "held for private review"):
                privacy.detect("alice@example.invalid uses ORDA", [], "http://127.0.0.1:18081/v1", "gemma")
            self.assertEqual(client.return_value.__enter__.return_value.post.call_count, 2)
            self.assertFalse(client.call_args.kwargs["trust_env"])
            self.assertFalse(client.call_args.kwargs["follow_redirects"])

    def test_local_structured_response_uses_existing_gemma_channel_workaround(self):
        text = "AlicePrivate uses ORDA."
        tokenized = httpx.Response(200, request=httpx.Request("POST", "http://127.0.0.1/tokenize"),
                                   json={"tokens": [42]})
        response = httpx.Response(200, request=httpx.Request("POST", "http://127.0.0.1/v1/chat/completions"),
                                  json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(
                                      {"uncertain": False, "spans": [{"text": "AlicePrivate", "kind": "username"}]})}}]})
        with patch.object(privacy.httpx, "Client") as client:
            post = client.return_value.__enter__.return_value.post
            post.side_effect = [tokenized, response]
            output, audit = privacy.detect(text, [{"user": "AlicePrivate", "text": "Question?"}],
                                           "http://127.0.0.1:18081/v1", "gemma", op_username="HiddenOP")
            self.assertEqual(output, "[redacted] uses ORDA.")
            self.assertEqual(len(audit["spans"]), 1)
            body = post.call_args_list[1].kwargs["json"]
            self.assertEqual(body["logit_bias"], [[42, False]])
            self.assertNotIn("chat_template_kwargs", body)
            self.assertIn("HiddenOP", json.loads(body["messages"][1]["content"])["private_identity_hints"])

    def test_failure_categories_distinguish_uncertainty_matching_shape_and_timeout(self):
        tokenized = httpx.Response(200, request=httpx.Request("POST", "http://127.0.0.1/tokenize"),
                                   json={"tokens": [42]})
        for content, finish, reason in (
                ('{"uncertain":true,"spans":[]}', "stop", "uncertain"),
                ('{"uncertain":false,"spans":[{"text":"Absent","kind":"person"}]}', "stop", "exact_text_absent"),
                ('{"uncertain":false,"spans":[]}', "length", "response_shape"),
                ("invalid JSON", "stop", "response_shape")):
            response = httpx.Response(200, request=httpx.Request("POST", "http://127.0.0.1/v1"),
                                      json={"choices": [{"finish_reason": finish, "message": {"content": content}}]})
            with self.subTest(reason=reason), patch.object(privacy.httpx, "Client") as client:
                client.return_value.__enter__.return_value.post.side_effect = [tokenized, response] * 2
                with self.assertRaises(privacy.DetectionFailure) as caught:
                    privacy.detect("Use ORDA.", [], "http://127.0.0.1:18081/v1", "gemma")
                self.assertEqual(caught.exception.reason, reason)
                self.assertEqual(caught.exception.model_calls, 2)
        with patch.object(privacy.httpx, "Client") as client:
            client.return_value.__enter__.return_value.post.side_effect = [
                tokenized, httpx.ReadTimeout("private response text"), tokenized, httpx.ReadTimeout("private response text")]
            with self.assertRaises(privacy.DetectionFailure) as caught:
                privacy.detect("Use ORDA.", [], "http://127.0.0.1:18081/v1", "gemma")
            self.assertEqual(caught.exception.reason, "chat_transport_timeout")
            self.assertNotIn("private response text", str(caught.exception))

if __name__ == "__main__":
    unittest.main()
