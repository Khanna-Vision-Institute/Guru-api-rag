#!/usr/bin/env python3
"""Staff-only live HTTP acceptance. No secret values, answers or transcripts printed.

Run on the managed server with boot-loaded credentials and one explicitly approved
public educational question in a private JSON input file. Makes read-only requests;
never creates, approves, retires, books, provisions or updates an assistant.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lacs_voice_preview import MODEL, SOURCE_FILES


class CheckFailed(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise CheckFailed("redirect_refused")


def request(base, path, header=None, body=None, accept="application/json"):
    parsed = urllib.parse.urlsplit(base)
    if (parsed.scheme != "https" or parsed.hostname not in ("khannainstitute.com", "staging.khannainstitute.com")
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/api/guru")):
        raise CheckFailed("invalid_origin")
    headers = {"Accept": accept}
    if header:
        headers[header[0]] = header[1]
    data = None if body is None else json.dumps(body).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(urllib.request.Request(base + path, data=data, headers=headers), timeout=15)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read(65537)
        if len(raw) > 65536:
            raise CheckFailed("response_limit")
        return response.code, response.headers.get_content_type(), dict(response.headers), raw


def json_response(result):
    status, content_type, _, raw = result
    if status != 200 or content_type != "application/json":
        raise CheckFailed("unexpected_http_response")
    return json.loads(raw)


def stream_text(result):
    status, content_type, _, raw = result
    if status != 200 or content_type != "text/event-stream":
        raise CheckFailed("stream_not_available")
    frames = [line[6:] for line in raw.decode("utf-8").splitlines() if line.startswith("data: ")]
    if not frames or frames[-1] != "[DONE]":
        raise CheckFailed("stream_incomplete")
    chunks = [json.loads(frame) for frame in frames[:-1]]
    if (not chunks or len({c["id"] for c in chunks}) != 1
            or chunks[-1]["choices"][0]["finish_reason"] != "stop"
            or any(c["model"] != MODEL or c["object"] != "chat.completion.chunk"
                   or len(c["choices"]) != 1 or "tool_calls" in c["choices"][0]["delta"] for c in chunks)):
        raise CheckFailed("stream_contract_invalid")
    return "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)


def verify(case, base, admin_key, voice_key, call=request):
    if (not isinstance(case, dict) or set(case) != {"question", "publicEducationConfirmed"}
            or case["publicEducationConfirmed"] is not True or not isinstance(case["question"], str)
            or not 1 <= len(case["question"]) <= 500):
        raise CheckFailed("invalid_public_case")
    if not admin_key or len(voice_key) < 32 or admin_key == voice_key:
        raise CheckFailed("distinct_server_credentials_required")
    voice_header = ("Authorization", "Bearer " + voice_key)
    unauth = call(base, "/vapi/lacs-preview/status")
    if unauth[0] != 401:
        raise CheckFailed("preview_authentication_not_proven")
    status = json_response(call(base, "/vapi/lacs-preview/status", voice_header))
    expected = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_FILES}
    if (status.get("protocol") != "lacs-voice-staff-preview-v1" or status.get("sourceSha256") != expected
            or status.get("consumerEnabled") is not True or status.get("deliveryMode") != "shadow"):
        raise CheckFailed("deployed_source_or_mode_mismatch")
    text = json_response(call(base, "/ask", ("X-Guru-Key", admin_key), {"query": case["question"], "top_k": 1}))
    if text.get("model_used") != "lacs-approved-qa" or not isinstance(text.get("answer"), str) or not text["answer"]:
        raise CheckFailed("text_not_from_approved_lacs")
    body = {"model": MODEL, "messages": [{"role": "user", "content": case["question"]}], "stream": False}
    voice = json_response(call(base, "/vapi/lacs-preview/chat/completions", voice_header, body))
    if voice["choices"][0]["message"]["content"] != text["answer"]:
        raise CheckFailed("text_voice_mismatch")
    streamed = stream_text(call(base, "/vapi/lacs-preview/chat/completions", voice_header,
                               {**body, "stream": True}, "text/event-stream"))
    if streamed != text["answer"]:
        raise CheckFailed("text_stream_mismatch")
    return {"schema": "lacs-voice-preview-acceptance-v1", "result": "pass",
            "sourceSha256": expected, "unauthenticatedDenied": True,
            "approvedTextEqualsVoiceJsonAndStream": True,
            "actualVapiCallVerified": False, "publicDeliveryEnabled": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, help="Private JSON: question and publicEducationConfirmed=true")
    args = parser.parse_args()
    try:
        raw = Path(args.case).read_bytes()
        if len(raw) > 4096:
            raise CheckFailed("case_limit")
        result = verify(json.loads(raw), os.getenv("GURU_PREVIEW_BASE_URL", "https://khannainstitute.com/api/guru").rstrip("/"),
                        os.getenv("GURU_ADMIN_KEY", ""), os.getenv("LACS_VOICE_PREVIEW_KEY", ""))
    except Exception as exc:
        reason = str(exc) if isinstance(exc, CheckFailed) else "acceptance_unavailable"
        print(json.dumps({"result": "fail", "reason": reason}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
