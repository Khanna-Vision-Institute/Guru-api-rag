"""Server-only LACS client. Local exact matching; no visitor text leaves this process.

Returns a reviewed suggestion, never authority for autonomous patient delivery.
Uses a deployment-supplied short-lived token provider; never browser credentials.
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

SCHEMA = "lacs-approved-qa-v1"
ID = re.compile(r"clinical-qa:[a-f0-9]{64}\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")


class Unavailable(Exception):
    """Safe, content-free failure. Callers must abstain; no legacy LLM fallback."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Unavailable("LACS redirect refused")


def _reference(row):
    if (not isinstance(row, dict) or not isinstance(row.get("documentId"), str)
            or not ID.fullmatch(row["documentId"])
            or type(row.get("version")) is not int or not 1 <= row["version"] <= 9007199254740991
            or not isinstance(row.get("integrityHash"), str) or not HASH.fullmatch(row["integrityHash"])):
        raise Unavailable("Invalid LACS reference")
    return {key: row[key] for key in ("documentId", "version", "integrityHash")}


def _normalize(text):
    # No fuzzy/semantic generalization of a potentially personal clinical question.
    return " ".join(text.casefold().split()).rstrip("?")


class ApprovedQaClient:
    def __init__(self, origin, token_provider):
        parsed = urllib.parse.urlsplit(origin)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.port not in (None, 443)):
            raise ValueError("A fixed trusted HTTPS origin is required")
        self._origin = origin.rstrip("/")
        self._token_provider = token_provider
        # No environment proxy may receive the Authorization header.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _request(self, path, body=None, deadline=None):
        remaining = min(3.0, deadline - time.monotonic())
        if remaining <= 0:
            raise Unavailable("LACS deadline exceeded")
        try:
            token = self._token_provider()
            if not isinstance(token, str) or not token or len(token) > 16384 or re.search(r"\s", token):
                raise Unavailable("LACS credential unavailable")
            headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
            payload = None if body is None else json.dumps(body).encode("utf-8")
            if payload is not None:
                headers["Content-Type"] = "application/json"
            request = urllib.request.Request(self._origin + path, data=payload, headers=headers)
            with self._opener.open(request, timeout=remaining) as response:
                if response.status != 200 or response.headers.get_content_type() != "application/json":
                    raise Unavailable("LACS response unavailable")
                # Socket deadline and bounded body; caller must also bound overall request time.
                raw = bytearray()
                while len(raw) <= 524288:
                    if time.monotonic() > deadline:
                        raise Unavailable("LACS deadline exceeded")
                    chunk = response.read1(min(4096, 524289 - len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
                if len(raw) > 524288 or time.monotonic() > deadline:
                    raise Unavailable("LACS response limit exceeded")
                value = json.loads(raw)
                if not isinstance(value, dict) or value.get("schemaVersion") != SCHEMA or value.get("requiresHumanReview") is not True:
                    raise Unavailable("Invalid LACS contract")
                return value
        except Exception:
            # Do not expose upstream bodies, credentials, URLs or raw exceptions to callers/logs.
            raise Unavailable("Approved knowledge unavailable") from None

    def suggest(self, question):
        """Return exact approved suggestion or None. Fetch fresh on every invocation.

        The caller must retain human review and its own urgent-symptom escalation.
        Do not put output into an LLM prompt as instruction, publish it automatically,
        or cache the answer for later delivery.
        """
        if not isinstance(question, str) or not question.strip() or len(question) > 500:
            return None
        deadline = time.monotonic() + 10
        after = None
        seen = set()
        matches = []
        for _ in range(20):
            path = "/v1/knowledge/approved-qa?limit=50"
            if after:
                path += "&after=" + urllib.parse.quote(after, safe="")
            page = self._request(path, deadline=deadline)
            if set(page) != {"schemaVersion", "requiresHumanReview", "items", "nextCursor"} or not isinstance(page["items"], list) or len(page["items"]) > 50:
                raise Unavailable("Invalid LACS catalog")
            for row in page["items"]:
                ref = _reference(row)
                if set(row) != {"documentId", "version", "integrityHash", "question", "keywords", "procedureTags"} or not isinstance(row["question"], str) or not 1 <= len(row["question"]) <= 500:
                    raise Unavailable("Invalid LACS catalog row")
                if ref["documentId"] in seen or (after and ref["documentId"] <= after):
                    raise Unavailable("Invalid LACS pagination")
                seen.add(ref["documentId"])
                if _normalize(row["question"]) == _normalize(question):
                    matches.append(row)
            cursor = page["nextCursor"]
            if cursor is None:
                break
            if not page["items"] or cursor != page["items"][-1]["documentId"]:
                raise Unavailable("Invalid LACS cursor")
            after = cursor
        else:
            raise Unavailable("LACS catalog exceeds bounded client capacity")
        if len(matches) != 1:
            return None
        selected = matches[0]
        ref = _reference(selected)
        result = self._request("/v1/knowledge/approved-qa/resolve", ref, deadline)
        if (result.get("schemaVersion") != SCHEMA or result.get("requiresHumanReview") is not True
                or set(result) != {"schemaVersion", "requiresHumanReview", "documentId", "version", "integrityHash", "question", "answer", "supportingUrls"}
                or _reference(result) != ref or result["question"] != selected["question"]
                or not isinstance(result["answer"], str) or not 1 <= len(result["answer"]) <= 4000
                or not isinstance(result["supportingUrls"], list) or len(result["supportingUrls"]) > 20
                or any(not isinstance(url, str) or not url.startswith("https://") for url in result["supportingUrls"])):
            raise Unavailable("Invalid LACS resolution")
        return result
