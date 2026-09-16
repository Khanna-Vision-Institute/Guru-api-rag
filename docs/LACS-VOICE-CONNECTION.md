# LACS → Guru → website and VAPI connection

## Status and the missing route

Raj requested completing the shared live connection on 2026-09-16 while he reviews
Q&A and records educational videos. This change supplies the missing voice
transport and executable HTTP acceptance; it does not claim patient delivery is
active or change the LACS v1 requirement for human review.

Inspected corporate KVI-Web commit `f66ab31c49d726e1ccc042a806ed1470e52d8c36`:

| Channel | Existing request path | Consequence |
| --- | --- | --- |
| Main website text widget | `/api/guru/vapi/webhook` | Reaches Guru, but legacy ecosystem/booking branches also need acceptance. |
| Older iGuru text widget | `/api/guru/ask` | Now protected; browser operation depends on an authenticated server proxy. Never add its key to JavaScript. |
| Browser speech-recognition/TTS mode | Text webhook, then `/tts` | Different from VAPI Web SDK voice; testing it does not prove a VAPI call. |
| VAPI Web SDK voice | Stored assistant ID from `/api/kvi-voice/config` | Provisioned assistants use OpenAI and their own static knowledge prompts. Their Guru tool is booking, not Q&A retrieval. |

The ten current personas are Brandi, Guru, Max, Lucy, Rose, Kate, Sage, Buffett,
Barbie and Jill. Changing Guru's `/ask` or a webhook alone cannot update these
stored VAPI assistants.

## New staff voice transport

Mount: `/vapi/lacs-preview`. Default is disabled. Authentication is a separate
server-only `Authorization: Bearer ...` credential from protected boot configuration.
It grants only this preview, never administration, booking, ingestion or approval.

- `GET /status`: authenticated protocol, boot-time hashes of the four connection
  source files, consumer enabled flag and delivery mode. No secret or source content.
- `POST /chat/completions`: OpenAI-compatible JSON or SSE response. The latest user
  question alone is matched locally by the same approved-Q&A consumer used by `/ask`.
- Valid current MATCH preserves approved wording. No-match, withdrawn/changed
  approval, ambiguity, outage, disabled consumer or invalid mode gives a fixed
  staff handoff. This transport never calls legacy search, a model or booking.
- Authorization precedes body reads. Body size, read time, message count, latest
  question length, concurrent requests and lookup duration are bounded. Disconnect
  cancels the awaiting request; the underlying consumer keeps timed-out workers
  counted until they finish. No payload, transcript or exception logging is added.
- Supplied system messages cannot change policy. Tools, customer metadata, query
  parameters and non-text messages are refused. `metadataSendMode=off` is required.

No existing public handler, assistant ID, website voice configuration or clinical
approval is changed. Existing shadow-mode fallback behavior remains unchanged.
Exact matching remains a limitation; voice paraphrases will usually hand off.

## Deployment and verification sequence for the server operator

1. Review and integrate Guru PRs #1, #2 and #3 in order, then this change. Preserve
   the release SHA and previous version. LACS coverage is already deployed; the
   latest verified LACS release is `2c766ed4cb3f1c07da5cc661d664353b18e0e3d7`,
   deployment run `35143365876`. Recheck current release at deployment time.
2. Use the existing protected AWS Secrets Manager bootstrap. Add a **distinct**
   random preview credential as `LACS_VOICE_PREVIEW_KEY` (32–256 ASCII characters,
   no whitespace) and `LACS_VOICE_PREVIEW_ENABLED=true`. Keep
   `LACS_CONSUMER_ENABLED=true`, `LACS_CONSUMER_DELIVERY=shadow` and the existing
   restricted LACS service identity. Do not paste keys in GitHub, chat, commands
   or browser code. Previously rotated credentials do not need rotating again.
3. Restart the managed environment loader and Guru service after the reviewed code
   is deployed. Confirm the HTTPS proxy forwards Authorization to these two exact
   paths without logging it, bodies or query strings. Keep request body limits at
   or below 32 KiB. The status fingerprint is captured at process startup; merely
   replacing files on disk is insufficient.
4. Choose one **currently approved public educational question** from LACS. Place
   it on the server in a private JSON file containing only `question` and
   `publicEducationConfirmed: true`. This is a test input, not new approval. Do not
   use patient conversations, medical records or an index dump.
5. Run the read-only check inside the existing managed service environment, with
   `GURU_ADMIN_KEY` and `LACS_VOICE_PREVIEW_KEY` already injected. Do not `source`
   the systemd EnvironmentFile in an interactive shell or print it.

   ```sh
   python3 -B scripts/verify_lacs_voice_preview.py --case /private/approved-public-case.json
   ```

   Default base is `https://khannainstitute.com/api/guru`; the optional nonsecret
   `GURU_PREVIEW_BASE_URL` also permits the exact staging hostname. The verifier
   refuses redirects, environment proxies and other hosts. Its report has no
   question, answer or credential. Pass means deployed connection-file hashes
   match the checkout and approved `/ask` wording equals voice JSON and SSE.
   It explicitly reports `actualVapiCallVerified=false` and
   `publicDeliveryEnabled=false` because HTTP tests do not prove audio delivery.
6. In the isolated staff VAPI environment, create a **new test assistant** using
   `deploy/vapi-lacs-staff-preview.json`. Configure the Custom LLM provider credential
   in VAPI's protected provider settings using the separate preview key. Its model
   URL is the base URL; VAPI appends `/chat/completions`. Confirm the actual request
   reaches that route with Bearer authorization. The JSON intentionally contains no
   key and is not a credential-complete provisioning request.
7. Keep this test assistant out of the website's public assistant-ID map and out of
   telephone/outbound campaigns. Disable alternate-model fallback and inherited
   tools/knowledge prompts. Test through VAPI's staff web-call interface with the
   same approved question. Verify the spoken answer, full completion, latency,
   interruption, no-match and unavailable-knowledge handoff. Do not count `/tts`
   playback or the HTTP verifier as the VAPI acceptance call. Check artifact/log
   settings in the actual VAPI account before using the test assistant.
8. Record only release hashes, source fingerprints, per-case pass/fail, latency and
   tested channel/persona. Use synthetic lifecycle fixtures for replacement and
   retirement checks in the isolated review environment; never retire Raj's real
   approvals merely to test. Keep recordings and transcripts disabled for these
   connection tests. Raj's separately recorded educational videos are unaffected.

## Work still required for public delivery

- Obtain the current Guru FAQ/OpenSearch **Q&A-only export**, plus nonsecret export
  time/source/version/count. The earlier spreadsheet and 31-row repository snapshot
  do not establish what is serving the reported 270-question live corpus.
- Reconcile conflicting, duplicate and withdrawn answers. Do not treat an outage,
  rejected version, known withdrawn question or ambiguous match as legacy fallback.
  Exact-only suppression does not cover paraphrases; reconcile and test those
  before enabling broad semantic fallback.
- Publish a reviewed LACS patient-delivery contract: v1 still returns
  `requiresHumanReview=true`. This preview must not reinterpret that as permission
  for unattended website/voice delivery. Raj's live-connection request authorizes
  preparing this work, not inventing clinical approval states.
- Validate the main website webhook before its ecosystem/booking branches, the
  older `/ask` proxy where still used, and each intended stored VAPI assistant.
  Complete approved-wording, urgent-symptom escalation, credential/identity, rate,
  privacy, withdrawal, monitoring and rollback acceptance before public cutover.
- Public fallback should use Guru only for a verified eligible uncovered question;
  reconciliation, emergency handling and patient delivery are not implemented by
  this transport patch. Live delivery remains blocked by the existing consumer.

Rollback the preview by setting `LACS_VOICE_PREVIEW_ENABLED=false`, restarting the
managed service, and disabling the isolated VAPI test assistant. No knowledge,
approval history or public assistant IDs need changing.

## Source references and testing scope

- [VAPI custom server integration and authentication](https://docs.vapi.ai/customization/custom-llm/using-your-server)
- [Official Custom LLM schema: base URL, metadata and timeouts](https://github.com/VapiAI/server-sdk-python/blob/main/src/vapi/types/custom_llm_model.py)
- [Official artifact controls](https://github.com/VapiAI/server-sdk-python/blob/main/src/vapi/types/artifact_plan.py)

Local tests exercise the ASGI request/response boundary, JSON/SSE equality,
authentication, bounds, disconnects, unsafe metadata, and the acceptance verifier's
failure cases with synthetic dependencies. They do not import main.py's external
clients, contact AWS/VAPI, certify the reverse proxy, prove audible speech or grant
patient-facing delivery. Run only the isolated suite documented in README.
