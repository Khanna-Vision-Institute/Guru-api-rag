#!/usr/bin/env python3
"""Read-only public text/voice transport acceptance; never prints content or keys."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from verify_lacs_voice_preview import request, json_response, stream_text, CheckFailed
from lacs_voice_preview import LacsVoicePublic, SOURCE_FILES
from lacs_approved_qa import PUBLIC_POLICY


def verify(case, base, key, call=request):
    if (not isinstance(case, dict) or set(case) != {"question", "publicEducationConfirmed"}
            or case["publicEducationConfirmed"] is not True or not isinstance(case["question"], str)
            or not 1 <= len(case["question"]) <= 500 or not 32 <= len(key) <= 256):
        raise CheckFailed("invalid_case_or_credential")
    root = "/vapi/lacs-public"
    auth = ("Authorization", "Bearer " + key)
    if call(base, root + "/status")[0] != 401:
        raise CheckFailed("voice_authentication_not_proven")
    status = json_response(call(base, root + "/status", auth))
    expected = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_FILES}
    if (status.get("protocol") != "lacs-voice-public-v1" or status.get("sourceSha256") != expected
            or status.get("deliveryMode") != "approved-public" or status.get("consumerEnabled") is not True):
        raise CheckFailed("deployed_source_or_mode_mismatch")
    text = json_response(call(base, "/vapi/webhook", body={"message": {"content": case["question"]}}))
    meta = text.get("lacs", {})
    if (meta.get("outcome") != "MATCH" or meta.get("requiresHumanReview") is not False
            or meta.get("deliveryPolicy") != PUBLIC_POLICY):
        raise CheckFailed("public_text_not_approved")
    answer = text["messages"][0]["text"]
    if not isinstance(answer, str) or not answer:
        raise CheckFailed("empty_answer")
    chat = json_response(call(base, "/guru/chat", body={"query": case["question"]}))
    if chat.get("answer") != answer:
        raise CheckFailed("guru_chat_mismatch")
    body = {"model": LacsVoicePublic.model, "messages": [{"role": "user", "content": case["question"]}]}
    voice = json_response(call(base, root + "/chat/completions", auth, body))
    streamed = stream_text(call(base, root + "/chat/completions", auth,
                               dict(body, stream=True), "text/event-stream"), LacsVoicePublic.model)
    if voice["choices"][0]["message"]["content"] != answer or streamed != answer:
        raise CheckFailed("public_text_voice_mismatch")
    # Invalid tool payload proves the public webhook cannot reach legacy booking.
    blocked = json_response(call(base, "/vapi/webhook", body={"toolCalls": [{"name": "synthetic-disabled-tool"}]}))
    if blocked.get("lacs", {}).get("outcome") != "BLOCKED":
        raise CheckFailed("tool_handoff_not_proven")
    return {"schema": "lacs-public-acceptance-v1", "result": "pass", "sourceSha256": expected,
            "approvedVersion": {key: meta[key] for key in ("documentId", "version", "integrityHash")},
            "publicHttpDeliveryVerified": True, "publicTextEqualsVoiceJsonAndStream": True,
            "actualVapiCallVerified": False, "allPersonaConfigurationsVerified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True)
    args = parser.parse_args()
    try:
        raw = Path(args.case).read_bytes()
        if len(raw) > 4096:
            raise CheckFailed("case_limit")
        result = verify(json.loads(raw), os.getenv("GURU_PREVIEW_BASE_URL", "https://khannainstitute.com/api/guru").rstrip("/"),
                        os.getenv("LACS_VOICE_PUBLIC_KEY", ""))
    except Exception as exc:
        print(json.dumps({"result": "fail", "reason": str(exc) if isinstance(exc, CheckFailed) else "acceptance_unavailable"}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
