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
FAQ/OpenSearch/LLM path, per `docs/operations/APPROVED-QA-CONSUMERS.md` in KVI-LACS-Core. `LACS_CONSUMER_DELIVERY`
is `shadow` (matches audited, visitor answers unchanged) until switched to `live`; staff requests with the admin key
always receive the approved answer.
