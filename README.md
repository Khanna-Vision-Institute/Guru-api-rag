# Guru RAG API

FastAPI service behind the Khanna Vision Institute website chat and Vapi voice assistants. Runs on EC2 (main AWS
account) as `guru-rag.service`; the website reaches it only through the site's `/api/guru` proxy.

## Runtime configuration and secrets

Secrets are **not** kept in this repository or in a `.env` file on the server. The server loads its configuration at boot
from AWS Secrets Manager (`guru-rag/env`, one JSON object) through `deploy/guru-env.service` → `deploy/guru-load-env.py`
→ `/run/guru/env`, which `guru-rag.service` reads via the drop-in `deploy/guru-rag-override.conf`. `env-template.txt`
documents every variable. To change a value: update the secret, then `sudo systemctl restart guru-env guru-rag`.

## Access control

`/logs`, `/docs`, `/openapi.json`, `/ingest-website`, `/embed` require header `X-Guru-Key` (admin key, else 404);
`/ask`, `/search` require a read-only key (e.g. the LACS consumer identity) or the admin key (else 401);
`/vapi/tool/*` requires `x-vapi-secret` (set on the Vapi assistant and the bookAppointment tool). Public chat
(`/vapi/webhook`, `/guru/chat`, `/tts`, `/health`) stays open for the website widget.

## LACS approved-Q&A consumer

`lacs_consumer.py` consults LACS (`/v1/knowledge/approved-qa`) with a dedicated OIDC service identity before the
FAQ/OpenSearch/LLM path, per `docs/operations/APPROVED-QA-CONSUMERS.md` in KVI-LACS-Core.

**This implementation is a shadow/staff-review integration, not authorized patient delivery.**
No deployment or environment change is required or performed by this patch. Keep the existing shadow setting.
The master switch defaults to disabled, and delivery defaults to shadow.

| Configuration | Behavior |
| --- | --- |
| Disabled, or delivery `off` | No LACS request, including staff requests; existing Guru path remains unchanged. |
| Enabled + `shadow`, public request | Consult LACS, log only channel/outcome, return a SHADOW decision without approved content; existing Guru path remains unchanged. |
| Enabled + `shadow`, admin `/ask` request | Staff can review an exact approved match. A completed no-match lookup uses the existing path; a verification failure returns a human-review handoff. |
| Enabled + `live`, or any unknown delivery value | BLOCKED without contacting LACS or invoking legacy Q&A. Live delivery has not been authorized. |

In staff review, MATCH returns the exact freshly resolved approved wording without LLM rewriting.
NO_MATCH means a successful complete catalog lookup found no exact match; it is not a network-error flag.
Ambiguity, invalid input, exhausted pagination, authentication failures, outages, deadlines, and rejected
fresh resolution (including a retired/changed version) are BLOCKED. All three Q&A entry points
(`/ask`, `/guru/chat`, `/vapi/webhook`) use the explicit decision. Unexpected consumer exceptions also block.
Public SHADOW mode deliberately preserves legacy answers even when the observed LACS result is BLOCKED;
this behavior is for testing only and is not the final clinical fallback policy.

Matching is local, exact after case/whitespace/question-mark normalization, not keyword or semantic matching.
The consumer sends only opaque references to LACS, not visitor questions. LACS requests are fresh, bounded,
and use a no-proxy/no-redirect client. At most two lookups can be outstanding, including timed-out workers;
requests do not build up an unbounded executor queue. Token responses are bounded and short-lived.
Consumer logs contain only fixed channel/outcome classes. This does not certify the privacy of pre-existing
Guru logging elsewhere in the application.

### Remaining gates before patient-facing LACS-first fallback

1. Deploy and verify LACS's retirement-aware coverage endpoint before this consumer is tested.
   The client checks coverage before catalog retrieval and again on an exact miss. A withdrawn
   or renamed historical question blocks fallback; only stable, complete, never-covered exact
   misses may return NO_MATCH. Missing/invalid/changed coverage blocks without downgrading.
   These are read-time checks, not a lease against later changes, and do not cover paraphrases.
   Live mode remains refused until all acceptance gates are complete.
2. Reconcile the current live Guru FAQ/OpenSearch corpus with approved LACS coverage, including conflicting,
   retired and duplicate answers. Obtain a questions-and-answers-only export, not conversations, credentials,
   patient records or an unrestricted index dump. The repository FAQ file is not evidence of the full live corpus.
3. Validate the dedicated service identity and version/retirement behavior in staging with synthetic questions,
   then approve patient-delivery rules, emergency escalation, monitoring and rollback separately.
   The LACS v1 contract still requires human review. Broader paraphrase/keyword matching also needs review.
4. Review/merge the underlying remediation PR and this focused follow-up in order. Do not auto-merge or deploy
   this follow-up, and do not activate live delivery during testing.

### Offline regression checks

Run only the isolated standard-library suite; older root test scripts can call external services.

```sh
python3 -B -m unittest discover -s tests/lacs -p 'test_*.py' -v
python3 -m py_compile lacs_approved_qa.py lacs_consumer.py main.py
```

Fixtures are synthetic. Handler tests compile only the real handler functions with stubbed dependencies,
avoiding main.py module-level service initialization. They verify the routing gates, not HTTP middleware,
the actual deployed server, authentication setup, website behavior, voice-provider fallback, or clinical accuracy.

### Historical coverage protocol

The consumer requires `GET /v1/knowledge/approved-qa/coverage` with schema
`lacs-approved-qa-coverage-v1`. It validates the complete sorted unique question-hash
set and revision before use. These suppression keys contain no answer or reviewer text.
Hashing and matching use ASCII-only case/whitespace normalization; Unicode is unchanged.
No visitor text or hash is sent to LACS. A missing endpoint blocks staff testing; public
shadow mode continues the unchanged legacy path and records only the observed outcome.
The authoritative contract, bounds and rollout sequence live in LACS's
`docs/operations/APPROVED-QA-CONSUMERS.md`. This is a follow-up stacked on Guru PR #2.
