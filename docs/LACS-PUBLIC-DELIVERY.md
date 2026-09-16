# Approved answers → Guru → website and Vapi

Raj authorized the live public connection on 2026-09-16, continuing the preceding
Guru upgrade conversation. Final clinical approval of public educational Q&A is
the content authorization; another human approval of each verbatim response is
not required in this lane. Saving, editing or holding a draft never publishes it.

## Behavior and limits

`LACS_CONSUMER_DELIVERY=approved-public` selects the new contract. Guru matches a
question locally, checks approval coverage, and calls the separately authenticated
LACS `/v1/knowledge/approved-qa/public-resolve` endpoint with only the exact document
ID, version and integrity hash. A verified `lacs-public-approved-qa-v1` response
with `deliveryPolicy=approved-public-education-exact-v1` permits copying the answer.
The old staff response with `requiresHumanReview=true` cannot activate delivery.

Every new request reads current approvals. An unapproved edit leaves the previous
approval active; approving the replacement changes the served wording on the next
successful fresh lookup. Retirement blocks it. No answer cache or model rewriting
is used. A change after fresh resolution can still race with response delivery;
this is a read-time check, not an atomic publishing lease.

The initial lane uses exact matching after ASCII case/whitespace/question-mark
normalization. Paraphrases, follow-up fragments, missing knowledge, stale versions,
ambiguous matches, withdrawn questions and outages hand off to KVI staff. This is
not semantic conversational retrieval and does not diagnose, infer candidacy or
provide personalized advice. Guru's reported 270-answer corpus still needs a
Q&A-only export/reconciliation before any legacy fallback can be added safely.

The website's existing text request shape is supported directly. Responses preserve
the selected website persona and its Vapi assistant mapping; LACS is reported as
the answer source, never as an unknown active-agent key. The public
webhook intercepts before legacy transcript logging, agent prompts and booking
branches. Consequently **booking through this chat lane also hands off to staff**;
the separate authenticated booking tool endpoints are unchanged. No booking,
outbound call, social posting or new clinical approval is performed by this release.

## Operator cutover — Nisha

1. Deploy the companion LACS public-resolve release through the existing protected
   staging application workflow after its exact commit passes full CI. It uses the
   existing service identity and schema 012; no new LACS secret or migration is
   needed. Verify readiness and authenticated public-resolve before changing Guru.
2. Deploy the reviewed Guru release (including merged PRs #1–#4) to the managed
   `guru-rag.service` server. Preserve the previous release and environment version.
   Keep keys in the existing `guru-rag/env` Secrets Manager record. Add a distinct
   random `LACS_VOICE_PUBLIC_KEY` (32–256 ASCII characters without whitespace),
   separate from admin and staff-preview keys. Use:

   ```text
   LACS_CONSUMER_ENABLED=true
   LACS_CONSUMER_DELIVERY=approved-public
   LACS_VOICE_PUBLIC_ENABLED=true
   ```

   Existing LACS origin, short-lived OIDC service credentials and coverage route
   remain unchanged. Restart `guru-env` and `guru-rag` after the reviewed files and
   managed configuration are installed. Do not source or print `/run/guru/env`.
3. Ensure the HTTPS `/api/guru` proxy forwards Authorization to the new voice
   route, suppresses body/query/Authorization logging, and limits bodies to 32 KiB.
   Public text works through `/api/guru/vapi/webhook`; `/ask` remains protected.
4. Put one currently approved general question into a private server JSON file:
   `question` plus `publicEducationConfirmed: true`. Inside the managed service
   environment run:

   ```sh
   python3 -B scripts/verify_lacs_public.py --case /private/approved-public-case.json
   ```

   This checks deployed source hashes, unauthenticated voice denial, live website
   text, `/guru/chat`, voice JSON and voice streaming equality, and blocked tool
   dispatch. It prints no question, answer, credential or transcript. A pass does
   **not** prove a real Vapi call or that each persona was reconfigured.
5. Apply the model block from `deploy/vapi-lacs-public.json` to each intended
   stored Vapi web assistant: Guru, Brandi, Max, Lucy, Rose, Kate, Sage, Buffett,
   Barbie and Jill. Preserve their IDs and voices. Use Custom LLM base URL
   `https://khannainstitute.com/api/guru/vapi/lacs-public`, model `kvi-lacs-public`,
   metadata off, no inherited tools/static knowledge or alternative model fallback,
   and the new server-only provider credential. Keep existing telephone/outbound
   campaigns out of this web-assistant cutover. The template contains no secret.
6. Test a real staff web call for every enabled persona before exposing it. Check
   the approved answer, completion, interruption, response time and no-match/error
   handoff. Keep test recording/transcript logging disabled. Raj's separately
   recorded educational videos are independent. Verify the actual website still
   selects the same stored assistant IDs; text/TTS playback is not a Vapi-call test.
7. Record release SHAs, source fingerprints, persona IDs, pass/fail and latency
   only. Monitor bounded match/block outcomes and service health. Use synthetic
   isolated lifecycle fixtures for replacement/withdrawal acceptance; do not alter
   Raj's real approved content to test the connection.

## Rollback

Keep `LACS_CONSUMER_DELIVERY=approved-public` and set `LACS_CONSUMER_ENABLED=false`
to make public text handoff-only; disable the Vapi public assistants/voice flag.
Do not use `off` or `shadow` as a safety rollback: those deliberately restore the
old legacy path. Preserve approval histories and credentials. No data reversal or
credential rotation is required by an ordinary application rollback.

## Evidence and provider reference

Offline tests use synthetic data and no service credentials. They exercise the
actual consumer, bounded ASGI voice transport and public webhook boundary, plus
contract rejection and withdrawal behavior. LACS also tests its actual TypeScript
feed against the cleanroom Python consumer and PostgreSQL lifecycle/isolation in CI.

Vapi's [official custom-server guide](https://docs.vapi.ai/customization/custom-llm/using-your-server)
documents Custom LLM and protected Authorization headers. Live account settings,
proxy behavior, clinical quality and audible voice acceptance require the operator.
