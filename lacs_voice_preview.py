"""Authenticated OpenAI-compatible transport for staff VAPI acceptance only.

No model, booking, legacy corpus, transcripts, or patient-delivery authority.
The existing LACS v1 response still requires human review. Keep preview assistants
in the staff VAPI environment, never in public website assistant configuration.
"""
import asyncio
import hmac
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

import lacs_consumer

MODEL = "kvi-lacs-staff-preview"
SOURCE_FILES = ("main.py", "lacs_consumer.py", "lacs_approved_qa.py", "lacs_voice_preview.py")
MAX_BODY = 32768
HANDOFF = "This staff preview has no verified approved answer to read. Please review this question with the KVI team."


class InvalidRequest(Exception):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidRequest()
        result[key] = value
    return result


def _question(body):
    allowed = {"model", "messages", "stream", "temperature", "max_tokens", "top_p",
               "frequency_penalty", "presence_penalty", "stream_options", "tools", "tool_choice"}
    if (not isinstance(body, dict) or set(body) - allowed or body.get("model") != MODEL
            or type(body.get("stream", False)) is not bool or body.get("tools", []) != []
            or body.get("tool_choice") not in (None, "none")):
        raise InvalidRequest()
    messages = body.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 32:
        raise InvalidRequest()
    for message in messages:
        if (not isinstance(message, dict) or set(message) != {"role", "content"}
                or message["role"] not in ("system", "user", "assistant")
                or not isinstance(message["content"], str) or len(message["content"]) > 4000):
            raise InvalidRequest()
    latest = messages[-1]
    if (latest["role"] != "user" or not latest["content"].strip()
            or len(latest["content"]) > 500):
        raise InvalidRequest()
    # History and supplied system text never become instructions or LACS input.
    return latest["content"]


class LacsVoicePreview:
    """ASGI app mounted at /vapi/lacs-preview; bounded before parsing the body."""

    def __init__(self, *, enabled=None, key=None, resolver=None):
        self.enabled = (os.getenv("LACS_VOICE_PREVIEW_ENABLED", "false") == "true"
                        if enabled is None else enabled)
        self.key = os.getenv("LACS_VOICE_PREVIEW_KEY", "") if key is None else key
        self.resolver = resolver or lacs_consumer.consult_async
        self.active = 0
        self.source_hashes = None
        if self.enabled:
            try:
                root_dir = Path(__file__).resolve().parent
                self.source_hashes = {name: hashlib.sha256((root_dir / name).read_bytes()).hexdigest() for name in SOURCE_FILES}
            except OSError:
                pass

    async def _json(self, send, status, value, outcome=None):
        headers = [(b"content-type", b"application/json"), (b"cache-control", b"no-store"),
                   (b"x-content-type-options", b"nosniff")]
        if outcome:
            headers += [(b"x-lacs-preview", b"staff-only"), (b"x-lacs-outcome", outcome.encode())]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": json.dumps(value, ensure_ascii=True).encode()})

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return
        path, root = scope.get("path", ""), scope.get("root_path", "")
        relative = path[len(root):] if root and path.startswith(root) else path
        route = (scope.get("method"), relative)
        if (not self.enabled or route not in (("POST", "/chat/completions"), ("GET", "/status"))
                or not isinstance(self.key, str) or not self.key.isascii()
                or not 32 <= len(self.key) <= 256 or any(c.isspace() for c in self.key)):
            await self._json(send, 404, {"error": "not_found"})
            return
        headers = scope.get("headers", [])
        auth = [v for k, v in headers if k.lower() == b"authorization"]
        if len(auth) != 1 or not hmac.compare_digest(auth[0], b"Bearer " + self.key.encode()):
            await self._json(send, 401, {"error": "unauthorized"})
            return
        if scope.get("query_string"):
            await self._json(send, 400, {"error": "invalid_request"})
            return
        if route == ("GET", "/status"):
            if self.source_hashes is not None:
                await self._json(send, 200, {"protocol": "lacs-voice-staff-preview-v1",
                                            "consumerEnabled": lacs_consumer.ENABLED,
                                            "deliveryMode": lacs_consumer.DELIVERY,
                                            "sourceSha256": self.source_hashes})
            else:
                await self._json(send, 503, {"error": "source_verification_unavailable"})
            return
        content_type = [v for k, v in headers if k.lower() == b"content-type"]
        lengths = [v for k, v in headers if k.lower() == b"content-length"]
        if (len(content_type) != 1
                or content_type[0].split(b";", 1)[0].strip().lower() != b"application/json"
                or any(k.lower() == b"content-encoding" for k, _ in headers)
                or len(lengths) > 1 or (lengths and (not lengths[0].isdigit() or int(lengths[0]) > MAX_BODY))):
            await self._json(send, 400, {"error": "invalid_request"})
            return
        # No await between admission check and increment on this ASGI event loop.
        if self.active >= 2:
            await self._json(send, 429, {"error": "preview_busy"})
            return
        self.active += 1
        try:
            try:
                raw = await asyncio.wait_for(self._body(receive), timeout=5)
                if raw is None:
                    return
                if lengths and len(raw) != int(lengths[0]):
                    raise InvalidRequest()
                body = json.loads(raw, object_pairs_hook=_unique_object,
                                  parse_constant=lambda _: (_ for _ in ()).throw(InvalidRequest()))
                question = _question(body)
                del raw
            except (InvalidRequest, ValueError, UnicodeError, RecursionError, asyncio.TimeoutError):
                await self._json(send, 400, {"error": "invalid_request"})
                return
            decision = await self._resolve(question, receive)
            if decision is None:
                return
            # SHADOW/BYPASS and NO_MATCH must never activate the unreconciled
            # legacy model through this new transport. Existing routes unchanged.
            text, outcome = HANDOFF, "BLOCKED"
            if decision.outcome == "MATCH" and isinstance(decision.suggestion, dict):
                suggestion = decision.suggestion
                answer = suggestion.get("answer")
                if (suggestion.get("requiresHumanReview") is True and isinstance(answer, str)
                        and 1 <= len(answer) <= 4000):
                    text, outcome = answer, "MATCH"
            elif decision.outcome == "NO_MATCH":
                outcome = "NO_MATCH"
            await self._completion(send, text, body.get("stream", False), outcome)
        finally:
            self.active -= 1

    @staticmethod
    async def _body(receive):
        raw = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return None
            if event["type"] != "http.request":
                raise InvalidRequest()
            raw.extend(event.get("body", b""))
            if len(raw) > MAX_BODY:
                raise InvalidRequest()
            if not event.get("more_body", False):
                return raw

    async def _resolve(self, question, receive):
        work = disconnect = None
        try:
            # Only the authenticated staff transport may request staff preview.
            work = asyncio.create_task(self.resolver(question, "voice-preview", privileged=True))
            disconnect = asyncio.create_task(self._disconnected(receive))
            done, _ = await asyncio.wait({work, disconnect}, timeout=11,
                                         return_when=asyncio.FIRST_COMPLETED)
            if disconnect in done:
                return None
            result = work.result() if work in done else lacs_consumer.blocked_decision()
            return result if isinstance(result, lacs_consumer.Decision) else lacs_consumer.blocked_decision()
        except asyncio.CancelledError:
            raise
        except Exception:
            # No exception text, transcript, credential or body in logs/responses.
            return lacs_consumer.blocked_decision()
        finally:
            for task in (work, disconnect):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(t for t in (work, disconnect) if t is not None), return_exceptions=True)

    @staticmethod
    async def _disconnected(receive):
        while (await receive()).get("type") != "http.disconnect":
            pass

    async def _completion(self, send, text, streaming, outcome):
        common = {"id": "chatcmpl-" + uuid.uuid4().hex, "created": int(time.time()), "model": MODEL}
        if not streaming:
            await self._json(send, 200, {**common, "object": "chat.completion", "choices": [
                {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
            ]}, outcome)
            return
        await send({"type": "http.response.start", "status": 200, "headers": [
            (b"content-type", b"text/event-stream"), (b"cache-control", b"no-store"),
            (b"x-accel-buffering", b"no"), (b"x-lacs-preview", b"staff-only"),
            (b"x-lacs-outcome", outcome.encode()), (b"x-content-type-options", b"nosniff")
        ]})
        # Resolve the complete approved version before emitting any answer bytes.
        chunks = [("assistant", "", None)] + [(None, text[i:i+200], None) for i in range(0, len(text), 200)]
        chunks += [(None, "", "stop")]
        for role, content, finish in chunks:
            delta = {"content": content}
            if role:
                delta["role"] = role
            payload = {**common, "object": "chat.completion.chunk", "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish}
            ]}
            await send({"type": "http.response.body", "body": b"data: " + json.dumps(payload).encode() + b"\n\n",
                        "more_body": True})
        await send({"type": "http.response.body", "body": b"data: [DONE]\n\n"})
