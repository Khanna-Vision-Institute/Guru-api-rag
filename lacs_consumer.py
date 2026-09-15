"""Guru-side LACS approved-Q&A consumer (server only).

Consults LACS (https://staging.khannainstitute.com) through the dedicated OIDC service identity BEFORE Guru's own
FAQ/OpenSearch/LLM path. Fail-closed: any error, timeout, 401/403/404/409/429 or ambiguity => None (abstain),
and the caller continues with its existing behaviour. No visitor text is logged here; only outcome classes.

Environment (server .env):
  LACS_CONSUMER_ENABLED=true|false      master switch (default false)
  LACS_CONSUMER_DELIVERY=off|shadow|live   off: never call LACS; shadow (default): call + audit a match but do NOT change
                                           the visitor answer; live: return the approved answer to the caller.
                                           Privileged callers (X-Guru-Key = GURU_ADMIN_KEY) always receive the answer,
                                           which is how staff verify the connection end-to-end without patient delivery.
  LACS_ORIGIN, LACS_TOKEN_URL, LACS_CLIENT_ID, LACS_CLIENT_SECRET   trusted server configuration (client credentials).
"""
import hmac
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

from lacs_approved_qa import ApprovedQaClient, Unavailable

log = logging.getLogger("lacs_consumer")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [LACS] %(message)s"))
    log.addHandler(_h)
log.setLevel(logging.INFO)

ENABLED = os.getenv("LACS_CONSUMER_ENABLED", "false").strip().lower() == "true"
DELIVERY = os.getenv("LACS_CONSUMER_DELIVERY", "shadow").strip().lower()
if DELIVERY not in ("off", "shadow", "live"):
    DELIVERY = "shadow"
ORIGIN = os.getenv("LACS_ORIGIN", "").strip()
TOKEN_URL = os.getenv("LACS_TOKEN_URL", "").strip()
CLIENT_ID = os.getenv("LACS_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("LACS_CLIENT_SECRET", "").strip()
ADMIN_KEY = os.getenv("GURU_ADMIN_KEY", "").strip()
OVERALL_DEADLINE_S = 10.5   # the LACS client budgets 10 s; the worker deadline must bound DNS/token time too

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="lacs")
_token_lock = threading.Lock()
_token_cache = {"value": None, "exp": 0.0}
_no_proxy_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_client = None
_client_error = None


def _token_provider():
    """Short-lived client-credentials token, cached until 30 s before expiry. Raises Unavailable on any failure."""
    now = time.monotonic()
    with _token_lock:
        if _token_cache["value"] and now < _token_cache["exp"] - 30:
            return _token_cache["value"]
        if not (TOKEN_URL.startswith("https://") and CLIENT_ID and CLIENT_SECRET):
            raise Unavailable("LACS credential not configured")
        data = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": CLIENT_ID,
                                       "client_secret": CLIENT_SECRET}).encode()
        req = urllib.request.Request(TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded",
                                                                     "Accept": "application/json"})
        try:
            with _no_proxy_opener.open(req, timeout=3.0) as r:
                body = json.loads(r.read(65536))
        except Exception:
            raise Unavailable("LACS token endpoint unavailable") from None
        token = body.get("access_token")
        ttl = body.get("expires_in", 0)
        if not isinstance(token, str) or not token or not isinstance(ttl, (int, float)) or ttl <= 0:
            raise Unavailable("LACS token response invalid")
        _token_cache["value"] = token
        _token_cache["exp"] = now + float(ttl)
        return token


def _get_client():
    global _client, _client_error
    if _client is None and _client_error is None:
        try:
            _client = ApprovedQaClient(ORIGIN, _token_provider)
        except ValueError as e:
            _client_error = str(e)
            log.warning("consumer disabled: %s", _client_error)
    return _client


def is_admin(request) -> bool:
    """True when the HTTP request carries the server admin key (staff verification path)."""
    try:
        given = request.headers.get("x-guru-key", "")
    except Exception:
        return False
    return bool(ADMIN_KEY) and hmac.compare_digest(given or "", ADMIN_KEY)


def format_answer(result, channel: str) -> str:
    text = result["answer"].strip()
    urls = [u for u in result.get("supportingUrls", []) if isinstance(u, str)]
    if channel == "ask" and urls:
        text += "\n\nSources:\n" + "\n".join(f"- {u}" for u in urls[:5])
    text += f"\n\n(Reviewed answer, version {result['version']}. For personal medical advice please speak with our team.)"
    return text


def consult(question: str, channel: str = "ask", privileged: bool = False):
    """Return {"text", "documentId", "version", "integrityHash", "supportingUrls"} or None (abstain)."""
    if not ENABLED:
        return None
    if DELIVERY == "off" and not privileged:
        return None
    client = _get_client()
    if client is None:
        return None
    started = time.monotonic()
    try:
        future = _executor.submit(client.suggest, question)
        result = future.result(timeout=OVERALL_DEADLINE_S)
    except FutureTimeout:
        log.info("channel=%s outcome=abstain reason=deadline ms=%d", channel, (time.monotonic() - started) * 1000)
        return None
    except Unavailable as e:
        log.info("channel=%s outcome=abstain reason=unavailable detail=%s ms=%d", channel, str(e)[:60], (time.monotonic() - started) * 1000)
        return None
    except Exception as e:  # never let the LACS lane break the caller
        log.info("channel=%s outcome=abstain reason=error type=%s", channel, type(e).__name__)
        return None
    if result is None:
        log.info("channel=%s outcome=no-match ms=%d", channel, (time.monotonic() - started) * 1000)
        return None
    deliver = DELIVERY == "live" or privileged
    log.info("channel=%s outcome=%s documentId=%s version=%s ms=%d", channel,
             "match-delivered" if deliver else "match-shadow", result["documentId"], result["version"],
             (time.monotonic() - started) * 1000)
    if not deliver:
        return None
    return {"text": format_answer(result, channel), "documentId": result["documentId"], "version": result["version"],
            "integrityHash": result["integrityHash"], "supportingUrls": list(result.get("supportingUrls", []))}
