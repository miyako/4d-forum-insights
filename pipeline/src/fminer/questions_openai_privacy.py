"""OpenAI PI span proposals for weekly deltas; never grants privacy clearance."""

import json
import os
import re
import time
import unicodedata

import httpx

from . import questions_privacy as privacy
from .questions_export import PATTERNS
from .voices import forbidden_names

ENDPOINT = "https://api.openai.com/v1/chat/completions"
MODEL_PATTERN = r"(?:gpt-6-luna|[A-Za-z0-9._-]+-\d{4}-\d{2}-\d{2})"
SCHEMA = {
    **privacy.SCHEMA,
    "properties": {
        **privacy.SCHEMA["properties"],
        "spans": {k: v for k, v in privacy.SCHEMA["properties"]["spans"].items() if k != "maxItems"},
    },
}
VERSION = "questions-openai-spans-v1-" + privacy.digest(
    [ENDPOINT, privacy.SYSTEM, SCHEMA, "minimal-hints-v1", 2400,
     MODEL_PATTERN, "gpt-6-luna:low"])[:12]


def detect(text, posts, model, timeout=120, attempts=2, op_username=None):
    if not isinstance(model, str) or not re.fullmatch(MODEL_PATTERN, model):
        raise ValueError("explicit supported OpenAI redaction model ID required")
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise ValueError("OpenAI redaction API key required")
    if not isinstance(text, str) or len(text) > 12000:
        raise privacy.ProposalError("display_context_bound")
    # Known secrets must never enter a hosted request, even for redaction.
    if any(re.search(PATTERNS[kind], text, re.IGNORECASE) for kind in ("credential", "token")):
        raise privacy.ProposalError("credential_in_display")
    normalized = unicodedata.normalize("NFKC", text).casefold()
    extra = [op_username] if op_username else []
    candidates = {p["user"] for p in posts} | set(extra) | forbidden_names(posts, usernames=extra)
    identities = []
    for name in sorted(candidates, key=lambda value: (-len(value), value)):
        folded = unicodedata.normalize("NFKC", name).casefold()
        if (folded and folded in normalized
                and not any(folded in unicodedata.normalize("NFKC", item).casefold() for item in identities)):
            identities.append(name)
    identities.sort()
    if sum(map(len, identities)) > 4000:
        raise privacy.ProposalError("identity_context_bound")
    if any(re.search(PATTERNS[kind], hint, re.IGNORECASE)
           for hint in identities for kind in ("credential", "token")):
        raise privacy.ProposalError("credential_in_hint")
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": privacy.SYSTEM},
                     {"role": "user", "content": json.dumps(
                         {"text": text, "private_identity_hints": identities}, ensure_ascii=False)}],
        "max_completion_tokens": 2400,
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "pi_spans", "strict": True, "schema": SCHEMA}},
    }
    if model == "gpt-6-luna":
        payload["reasoning_effort"] = "low"
    calls, reason, usage = 0, "validation", {"prompt_tokens": 0, "completion_tokens": 0}
    usage_reported_calls = 0
    deadline = time.monotonic() + timeout
    with httpx.Client(follow_redirects=False, trust_env=False,
                      headers={"Authorization": "Bearer " + key}) as client:
        for _ in range(attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = "timeout"
                break
            try:
                calls += 1
                with client.stream("POST", ENDPOINT, json=payload, timeout=remaining) as response:
                    response.raise_for_status()
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        if time.monotonic() >= deadline:
                            raise privacy.ProposalError("timeout")
                        data.extend(chunk)
                        if len(data) > 256000:
                            raise privacy.ProposalError("response_oversized")
                body = json.loads(data)
                if not isinstance(body, dict):
                    raise privacy.ProposalError("response_shape")
                response_usage = body.get("usage")
                if isinstance(response_usage, dict) and all(type(response_usage.get(k)) is int and response_usage[k] >= 0
                                                           for k in usage):
                    for kind in usage:
                        usage[kind] += response_usage[kind]
                    usage_reported_calls += 1
                if body.get("model") != model:
                    raise privacy.ProposalError("model_identity")
                choice = body["choices"][0]
                if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                    raise privacy.ProposalError("response_shape")
                if choice["finish_reason"] != "stop" or choice["message"].get("refusal"):
                    raise privacy.ProposalError("incomplete_or_refused")
                result = json.loads(choice["message"]["content"])
                cleared, spans = privacy.resolve_proposals(text, result)
                if privacy.findings(cleared, posts, op_username):
                    raise privacy.ProposalError("residual_identifiers")
                if not isinstance(response_usage, dict) or any(type(response_usage.get(k)) is not int
                                                               or response_usage[k] < 0 for k in usage):
                    raise privacy.ProposalError("usage_missing")
                return cleared, {"response_hash": privacy.digest(result), "spans": spans, "model_calls": calls,
                                 "original_hash": privacy.digest(text), "display_hash": privacy.digest(cleared),
                                 "usage": usage, "usage_unreported_calls": calls - usage_reported_calls}
            except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as error:
                reason = getattr(error, "reason", "response_or_transport")
                if isinstance(error, httpx.TimeoutException):
                    reason = "timeout"
                elif isinstance(error, httpx.HTTPStatusError):
                    reason = "http_" + str(error.response.status_code)
    failure = privacy.DetectionFailure(calls, reason)
    failure.usage = usage
    failure.usage_unreported_calls = calls - usage_reported_calls
    raise failure
