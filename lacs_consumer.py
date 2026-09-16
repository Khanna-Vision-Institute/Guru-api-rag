"""Server-only LACS shadow consumer with explicit routing decisions.

None from the validated client means a completed catalog with no exact match.
Errors, ambiguous matches and failed fresh resolution are BLOCKED, never NO_MATCH.
Public SHADOW calls intentionally retain the existing Guru answer path, even when
the observed outcome is BLOCKED; this is a test mode, not live clinical fallback.

Live delivery is deliberately unavailable until LACS supplies retirement-aware
coverage, the legacy corpus is reconciled, and patient delivery is authorized.
Server configuration comes from the deployment's protected environment.
"""
import asyncio
import hmac
import json
import logging
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from lacs_approved_qa import ApprovedQaClient, Unavailable, _NoRedirect

log = logging.getLogger("lacs_consumer")
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("[LACS] %(message)s"))
    log.addHandler(_handler)
log.setLevel(logging.INFO)

ENABLED = os.getenv("LACS_CONSUMER_ENABLED", "false").strip().lower() == "true"
DELIVERY = os.getenv("LACS_CONSUMER_DELIVERY", "shadow").strip().lower()
ORIGIN = os.getenv("LACS_ORIGIN", "").strip()
TOKEN_URL = os.getenv("LACS_TOKEN_URL", "").strip()
CLIENT_ID = os.getenv("LACS_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("LACS_CLIENT_SECRET", "").strip()
ADMIN_KEY = os.getenv("GURU_ADMIN_KEY", "").strip()
OVERALL_DEADLINE_S = 10.5

HANDOFF_TEXT = "I cannot verify an approved answer right now. Please contact the KVI team for help."


@dataclass(frozen=True)
class Decision:
    outcome: str
    reason: str = ""
    suggestion: dict | None = None
    observed_outcome: str | None = None

    @property
    def allow_legacy(self):
        # BYPASS and SHADOW are explicit operational modes, not clinical misses.
        return self.outcome in ("BYPASS", "SHADOW", "NO_MATCH")

    @property
    def text(self):
        if self.outcome == "MATCH" and self.suggestion:
            # Preserve reviewed wording. Never send it through the legacy LLM.
            return self.suggestion["answer"]
        return HANDOFF_TEXT


_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="lacs")
_slots = threading.BoundedSemaphore(2)
_token_lock = threading.Lock()
_token_cache = {"value": None, "exp": 0.0}
_no_proxy_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
_client = None


def _token_provider():
    """Bounded short-lived token response; no redirects, proxy or raw error logs."""
    with _token_lock:
        now = time.monotonic()
        if _token_cache["value"] and now < _token_cache["exp"] - 30:
            return _token_cache["value"]
        try:
            parsed = urllib.parse.urlsplit(TOKEN_URL)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                    or parsed.password or parsed.query or parsed.fragment
                    or parsed.port not in (None, 443) or any(c.isspace() for c in TOKEN_URL)
                    or not CLIENT_ID or not CLIENT_SECRET):
                raise Unavailable("LACS credential not configured")
            data = urllib.parse.urlencode({
                "grant_type": "client_credentials", "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            }).encode()
            req = urllib.request.Request(TOKEN_URL, data=data, headers={
                "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
            })
            with _no_proxy_opener.open(req, timeout=3.0) as response:
                if response.status != 200 or response.headers.get_content_type() != "application/json":
                    raise Unavailable("Invalid token response")
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise Unavailable("Oversized token response")
                body = json.loads(raw)
            if not isinstance(body, dict):
                raise Unavailable("Invalid token response")
            token, ttl = body.get("access_token"), body.get("expires_in")
            if (not isinstance(token, str) or not token or len(token) > 16384
                    or any(c.isspace() for c in token) or type(ttl) not in (int, float)
                    or not math.isfinite(ttl) or not 0 < ttl <= 3600):
                raise Unavailable("Invalid token response")
        except Exception:
            raise Unavailable("LACS token unavailable") from None
        _token_cache["value"] = token
        _token_cache["exp"] = now + float(ttl)
        return token


def _get_client():
    global _client
    if _client is None:
        _client = ApprovedQaClient(ORIGIN, _token_provider)
    return _client


def is_admin(request) -> bool:
    try:
        given = request.headers.get("x-guru-key", "")
        return bool(ADMIN_KEY) and hmac.compare_digest(given or "", ADMIN_KEY)
    except Exception:
        return False


def blocked_decision():
    return Decision("BLOCKED", "verification_unavailable")


def _mode():
    if not ENABLED or DELIVERY == "off":
        return Decision("BYPASS", "disabled")
    if DELIVERY != "shadow":
        # Even explicit 'live' must not bypass the still-open acceptance gates.
        return Decision("BLOCKED", "live_delivery_not_authorized")
    return None


def _present(decision, channel, privileged):
    safe_channel = channel if channel in ("ask", "chat", "webhook") else "unknown"
    log.info("channel=%s outcome=%s", safe_channel, decision.outcome)
    if not privileged:
        # No reviewed content or record metadata escapes into public shadow mode.
        return Decision("SHADOW", "staff_test_only", observed_outcome=decision.outcome)
    return decision


def _submit(question):
    # ThreadPoolExecutor's own queue is unbounded. Admit at most two jobs total,
    # including timed-out jobs still finishing DNS/token/network operations.
    if not _slots.acquire(blocking=False):
        raise Unavailable("LACS workers busy")
    slots = _slots
    try:
        future = _executor.submit(_get_client().suggest, question)
    except Exception:
        slots.release()
        raise
    future.add_done_callback(lambda _: slots.release())
    return future


def _resolved(result):
    return Decision("NO_MATCH", "complete_catalog_no_exact_match") if result is None else Decision("MATCH", suggestion=result)


def consult(question: str, channel: str = "ask", privileged: bool = False):
    mode = _mode()
    if mode is not None:
        return mode
    future = None
    try:
        future = _submit(question)
        decision = _resolved(future.result(timeout=OVERALL_DEADLINE_S))
    except Exception:
        if future is not None:
            future.cancel()
        decision = blocked_decision()
    return _present(decision, channel, privileged)


async def consult_async(question: str, channel: str = "webhook", privileged: bool = False):
    mode = _mode()
    if mode is not None:
        return mode
    future = None
    try:
        future = _submit(question)
        result = await asyncio.wait_for(asyncio.wrap_future(future), timeout=OVERALL_DEADLINE_S)
        decision = _resolved(result)
    except asyncio.CancelledError:
        if future is not None:
            future.cancel()
        raise
    except Exception:
        if future is not None:
            future.cancel()
        decision = blocked_decision()
    return _present(decision, channel, privileged)
