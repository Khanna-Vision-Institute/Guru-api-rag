"""Bounded public text lane. No legacy logging, model, tools or booking dispatch."""
import asyncio
import json

import lacs_consumer
from lacs_voice_preview import InvalidRequest, _unique_object


def _text(body):
    if not isinstance(body, dict):
        raise InvalidRequest()
    # The current KVI widget sends message.content and call.id. Ignore the
    # bounded session ID; it is never logged, retained, or sent to LACS.
    if set(body) <= {"message", "call"} and "message" in body:
        message = body["message"]
        call = body.get("call", {})
        if (not isinstance(message, dict) or set(message) != {"content"}
                or not isinstance(call, dict) or set(call) - {"id"}
                or ("id" in call and (not isinstance(call["id"], str) or len(call["id"]) > 256))):
            raise InvalidRequest()
        question = message["content"]
    elif set(body) <= {"text", "session_id"} and "text" in body:
        question = body["text"]
    else:
        # Provider events and tool requests cannot fall through to the old
        # unauthenticated booking branches in approved-public mode.
        raise InvalidRequest()
    if not isinstance(question, str) or not question.strip() or len(question) > 500:
        raise InvalidRequest()
    return question


async def public_webhook(request, *, rate_limit=None):
    decision = lacs_consumer.blocked_decision()
    try:
        if rate_limit is not None:
            peer = request.client.host if request.client else "unknown"
            client_ip = request.headers.get("x-forwarded-for", peer).split(",")[0].strip()
            if rate_limit(client_ip):
                raise InvalidRequest()
        if request.url.query or request.headers.get("content-encoding"):
            raise InvalidRequest()
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise InvalidRequest()
        async def read():
            raw = bytearray()
            async for part in request.stream():
                raw.extend(part)
                if len(raw) > 32768:
                    raise InvalidRequest()
            return raw
        raw = await asyncio.wait_for(read(), 5)
        body = json.loads(raw, object_pairs_hook=_unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(InvalidRequest()))
        question = _text(body)
        del raw, body
        decision = await lacs_consumer.consult_async(question, "webhook")
        if (decision.outcome != "MATCH" or not isinstance(decision.suggestion, dict)
                or decision.suggestion.get("schemaVersion") != lacs_consumer.PUBLIC_SCHEMA
                or decision.suggestion.get("requiresHumanReview") is not False
                or decision.suggestion.get("deliveryPolicy") != lacs_consumer.PUBLIC_POLICY):
            decision = lacs_consumer.blocked_decision()
    except asyncio.CancelledError:
        raise
    except Exception:
        decision = lacs_consumer.blocked_decision()
    metadata = {"outcome": decision.outcome, "requiresHumanReview": decision.outcome != "MATCH"}
    if decision.outcome == "MATCH":
        metadata.update({key: decision.suggestion[key] for key in
                         ("documentId", "version", "integrityHash", "supportingUrls", "deliveryPolicy")})
    return {"messages": [{"type": "text", "text": decision.text}], "active_agent": "lacs", "lacs": metadata}
