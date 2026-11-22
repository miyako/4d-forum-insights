"""Synthetic-only tests of hosted privacy request minimization and validation."""

import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline/src"))
from fminer import questions_openai_privacy as hosted, questions_privacy as privacy
from fminer import questions_openai_review as independent

MODEL = "synthetic-2026-01-01"


class HostedPrivacyTests(unittest.TestCase):
    def run_detector(self, handler, text="AlicePrivate uses ORDA.", posts=None, model=MODEL, **kwargs):
        requests = []
        def capture(request):
            requests.append(request)
            return handler(request)
        client = httpx.Client(transport=httpx.MockTransport(capture), follow_redirects=False, trust_env=False)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-key"}), \
                patch.object(hosted.httpx, "Client", return_value=client):
            result = hosted.detect(text, posts or [{"user": "AlicePrivate", "text": "PRIVATE RAW POST"}],
                                   model, **kwargs)
        return result, requests

    @staticmethod
    def response(result, model=MODEL):
        return httpx.Response(200, json={"model": model, "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                              "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]})

    def test_only_matching_hints_and_display_are_sent_and_spans_applied(self):
        result = {"uncertain": False, "spans": [{"text": "AlicePrivate", "kind": "username"}]}
        (output, audit), requests = self.run_detector(
            lambda _: self.response(result),
            posts=[{"user": "AlicePrivate", "text": "PRIVATE RAW POST"}, {"user": "HiddenOther", "text": "SECRET POST"}])
        self.assertEqual(output, "[redacted] uses ORDA.")
        self.assertEqual(audit["usage"], {"prompt_tokens": 10, "completion_tokens": 5})
        body = json.loads(requests[0].content)
        self.assertEqual(requests[0].url, hosted.ENDPOINT)
        data = json.loads(body["messages"][1]["content"])
        self.assertEqual(data, {"text": "AlicePrivate uses ORDA.", "private_identity_hints": ["AlicePrivate"]})
        self.assertNotIn("PRIVATE RAW POST", requests[0].content.decode())
        self.assertNotIn("HiddenOther", requests[0].content.decode())
        self.assertEqual(body["response_format"]["type"], "json_schema")

    def test_documented_luna_snapshot_uses_bounded_reasoning_and_exact_identity(self):
        proposal = {"uncertain": False, "spans": [{"text": "AlicePrivate", "kind": "username"}]}
        (output, _), requests = self.run_detector(
            lambda _: self.response(proposal, model="gpt-6-luna"), model="gpt-6-luna")
        self.assertEqual(output, "[redacted] uses ORDA.")
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["model"], "gpt-6-luna")
        self.assertEqual(payload["reasoning_effort"], "low")
        with self.assertRaises(privacy.DetectionFailure):
            self.run_detector(lambda _: self.response(proposal, model="other"),
                              model="gpt-6-luna", attempts=1)

    def test_known_secrets_are_held_before_any_network_request(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-key"}), \
                patch.object(hosted.httpx, "Client", side_effect=AssertionError("network attempted")):
            with self.assertRaises(privacy.ProposalError) as caught:
                hosted.detect("password=hunter2", [], MODEL)
            self.assertEqual(caught.exception.reason, "credential_in_display")
            with self.assertRaises(privacy.ProposalError):
                hosted.detect("Use ghp_1234567890123456789012345", [], MODEL)

    def test_independent_review_sends_no_raw_posts_and_rejection_is_not_retried_into_acceptance(self):
        requests = []
        decision = {"privacy_clear": True, "meaning_preserved": False, "uncertain": False}
        def capture(request):
            requests.append(request)
            return self.response(decision)
        client = httpx.Client(transport=httpx.MockTransport(capture), follow_redirects=False, trust_env=False)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-key"}), \
                patch.object(hosted.httpx, "Client", return_value=client):
            result, audit = independent.review("AlicePrivate uses ORDA.", "[redacted] uses ORDA.",
                                              [{"user": "AlicePrivate", "text": "PRIVATE RAW POST"}], MODEL)
        self.assertFalse(result["meaning_preserved"])
        self.assertEqual(len(requests), 1)
        payload = json.loads(requests[0].content)
        self.assertNotEqual(payload["messages"][0]["content"], privacy.SYSTEM)
        data = json.loads(payload["messages"][1]["content"])
        self.assertEqual(data["private_identity_hints"], ["AlicePrivate"])
        self.assertNotIn("PRIVATE RAW POST", requests[0].content.decode())
        self.assertEqual(audit["version"], independent.VERSION)
        with patch.object(hosted.httpx, "Client", side_effect=AssertionError("network attempted")):
            with self.assertRaises(privacy.ProposalError):
                independent.review("password=synthetic", "[redacted]", [], MODEL)

    def test_ambiguous_uncertain_refusal_and_unpinned_model_fail_closed(self):
        for result in ({"uncertain": True, "spans": []},
                       {"uncertain": False, "spans": [{"text": "Alice", "kind": "person"}]}):
            with self.subTest(result=result), self.assertRaises(privacy.DetectionFailure) as caught:
                self.run_detector(lambda _: self.response(result), text="Alice and Alice", attempts=2)
            self.assertEqual(caught.exception.model_calls, 2)
        with self.assertRaises(privacy.DetectionFailure):
            self.run_detector(lambda _: self.response({"uncertain": False, "spans": []}, model="other"))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-key"}):
            with self.assertRaises(ValueError):
                hosted.detect("Use ORDA.", [], "unversioned-model")

    def test_residual_identifiers_are_held(self):
        with self.assertRaises(privacy.DetectionFailure) as caught:
            self.run_detector(lambda _: self.response({"uncertain": False, "spans": []}))
        self.assertEqual(caught.exception.reason, "residual_identifiers")
        self.assertEqual(caught.exception.usage, {"prompt_tokens": 20, "completion_tokens": 10})
        self.assertEqual(caught.exception.usage_unreported_calls, 0)

    def test_bad_provider_response_is_held_without_body_in_exception(self):
        for response in (httpx.Response(200, json=["private"]), httpx.Response(200, json={
                "model": MODEL, "choices": [{"message": []}]})):
            with self.subTest(response=response), self.assertRaises(privacy.DetectionFailure) as caught:
                self.run_detector(lambda _: response, attempts=1)
            self.assertEqual(caught.exception.reason, "response_shape")
            self.assertNotIn("private", caught.exception.reason)

    def test_partial_usage_is_recorded_without_inventing_timed_out_call_cost(self):
        calls = []
        def responder(request):
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ReadTimeout("private details")
            return self.response({"uncertain": False, "spans": [{"text": "AlicePrivate", "kind": "username"}]})
        (_, audit), _ = self.run_detector(responder)
        self.assertEqual(audit["model_calls"], 2)
        self.assertEqual(audit["usage"], {"prompt_tokens": 10, "completion_tokens": 5})
        self.assertEqual(audit["usage_unreported_calls"], 1)


if __name__ == "__main__":
    unittest.main()
