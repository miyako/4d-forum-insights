"""OpenAI PI span proposals for weekly deltas; never grants privacy clearance."""

import json
import os
import re
import time
from pathlib import Path

import httpx

from . import questions_privacy as privacy
from .questions_export import PATTERNS

ENDPOINT = "https://api.openai.com/v1/chat/completions"
MODEL_PATTERN = r"(?:gpt-6-luna|[A-Za-z0-9._-]+-\d{4}-\d{2}-\d{2})"
SCHEMA = {
    **privacy.SCHEMA,
    "properties": {
        **privacy.SCHEMA["properties"],
        "spans": {k: v for k, v in privacy.SCHEMA["properties"]["spans"].items() if k != "maxItems"},
    },
}
SYSTEM = privacy.SYSTEM.replace("The text and identities are", "The text is") + (
    " Judge words in context: a common noun is not a personal name merely because it can also be used as one."
)
VERSION = "questions-openai-spans-v2-" + privacy.digest(
    [ENDPOINT, SYSTEM, SCHEMA, "cleared-display-hashes-v1", 2400,
     MODEL_PATTERN, "gpt-6-luna:low"])[:12]


def check_text(text):
    if not isinstance(text, str) or len(text) > 12000:
        raise privacy.ProposalError("display_context_bound")
    if any(re.search(PATTERNS[kind], text, re.IGNORECASE) for kind in ("credential", "token")):
        raise privacy.ProposalError("credential_in_display")


def load_cleared_hashes(root):
    path = Path(root) / "hosted-input-clearance.json"
    if not path.exists():
        raise ValueError("missing hosted-input-clearance.json; no display fields sent")
    record = json.loads(path.read_text())
    if (not isinstance(record, dict) or record.get("schema") != "questions-hosted-input-clearance-v1"
            or not isinstance(record.get("hashes"), list) or not record["hashes"]
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                   for value in record["hashes"])):
        raise ValueError("invalid hosted input clearance")
    return frozenset(record["hashes"])


def check_cleared(text, cleared_hashes):
    check_text(text)
    if privacy.digest(text) not in cleared_hashes:
        raise privacy.ProposalError("hosted_input_not_cleared")


def complete(system, data, schema, model, validate, timeout=120, attempts=2):
    if not isinstance(model, str) or not re.fullmatch(MODEL_PATTERN, model):
        raise ValueError("explicit supported OpenAI model ID required")
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise ValueError("OpenAI API key required")
    if timeout <= 0 or type(attempts) is not int or not 1 <= attempts <= 2:
        raise ValueError("invalid OpenAI request bounds")
    content = json.dumps(data, ensure_ascii=False)
    if len(content) > 32000:
        raise privacy.ProposalError("review_context_bound")
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": content}],
        "max_completion_tokens": 2400,
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "display_review", "strict": True, "schema": schema}},
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
                validated = validate(result)
                if not isinstance(response_usage, dict) or any(type(response_usage.get(k)) is not int
                                                               or response_usage[k] < 0 for k in usage):
                    raise privacy.ProposalError("usage_missing")
                return validated, {"response_hash": privacy.digest(result), "model": model,
                                   "model_calls": calls, "usage": usage,
                                   "usage_unreported_calls": calls - usage_reported_calls}
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


def detect(text, posts, model, timeout=120, attempts=2, op_username=None, *, cleared_hashes=frozenset()):
    check_cleared(text, cleared_hashes)

    def validate(result):
        cleared, spans = privacy.resolve_proposals(text, result)
        if privacy.findings(cleared, posts, op_username):
            raise privacy.ProposalError("residual_identifiers")
        return cleared, spans

    (cleared, spans), audit = complete(
        SYSTEM, {"text": text}, SCHEMA, model,
        validate, timeout, attempts)
    audit.update(spans=spans, original_hash=privacy.digest(text), display_hash=privacy.digest(cleared))
    return cleared, audit
