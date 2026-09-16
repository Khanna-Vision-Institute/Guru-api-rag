"""Synthetic public delivery contract, routing and voice boundary checks."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import lacs_consumer as consumer
from lacs_approved_qa import ApprovedQaClient, PUBLIC_SCHEMA, PUBLIC_POLICY, Unavailable
from lacs_public_webhook import public_webhook
from lacs_voice_preview import LacsVoicePublic
from test_client import ANSWER, PAGE, ROW, REF
import test_voice_preview as preview_tests
KEY = preview_tests.KEY

PUBLIC = dict(ANSWER, schemaVersion=PUBLIC_SCHEMA, requiresHumanReview=False, deliveryPolicy=PUBLIC_POLICY)


class PublicClientTests(unittest.TestCase):
    def test_public_resolution_is_separate_and_never_accepts_staff_authority(self):
        client = ApprovedQaClient("https://lacs.example.invalid", lambda: "synthetic")
        client._coverage = Mock(return_value={"revision": "synthetic", "questionHashes": []})
        for answer in (PUBLIC, ANSWER, dict(PUBLIC, deliveryPolicy="wrong"), dict(PUBLIC, requiresHumanReview=True)):
            with patch.object(client, "_request", side_effect=[PAGE, answer]) as call:
                if answer == PUBLIC:
                    self.assertEqual(client.suggest(ROW["question"], public_delivery=True), PUBLIC)
                else:
                    with self.assertRaises(Unavailable):
                        client.suggest(ROW["question"], public_delivery=True)
                self.assertEqual(call.call_args.args[:2], ("/v1/knowledge/approved-qa/public-resolve", REF))

    def test_public_consumer_blocks_every_miss_failure_or_staff_result(self):
        client = Mock()
        with patch.multiple(consumer, ENABLED=True, DELIVERY="approved-public"), patch.object(consumer, "_get_client", return_value=client):
            for answer in (PUBLIC, None, ANSWER, Unavailable("PRIVATE-CANARY")):
                client.suggest.side_effect = answer if isinstance(answer, Exception) else None
                client.suggest.return_value = answer
                for privileged in (False, True):
                    decision = consumer.consult(ROW["question"], privileged=privileged)
                    self.assertEqual(decision.outcome, "MATCH" if answer == PUBLIC else "BLOCKED")
                    self.assertFalse(decision.allow_legacy)
                    self.assertNotIn("PRIVATE-CANARY", decision.text)
            client.suggest.assert_called_with(ROW["question"], public_delivery=True)

    def test_public_disable_is_handoff_not_legacy_rollback(self):
        with patch.multiple(consumer, ENABLED=False, DELIVERY="approved-public"), patch.object(consumer, "_submit") as submit:
            decision = consumer.consult(ROW["question"])
            self.assertEqual(decision.outcome, "BLOCKED")
            self.assertFalse(decision.allow_legacy)
            submit.assert_not_called()


class PublicTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_widget_payload_preserves_wording_without_legacy(self):
        async def stream():
            yield json.dumps({"message": {"content": ROW["question"]}, "call": {"id": "synthetic-session"}}).encode()
        request = SimpleNamespace(headers={"content-type": "application/json"}, url=SimpleNamespace(query=""), stream=stream)
        with patch.object(consumer, "consult_async", new=AsyncMock(return_value=consumer.Decision("MATCH", suggestion=PUBLIC))) as resolve:
            result = await public_webhook(request)
        self.assertEqual(result["messages"][0]["text"], PUBLIC["answer"])
        self.assertFalse(result["lacs"]["requiresHumanReview"])
        resolve.assert_awaited_once_with(ROW["question"], "webhook")
        self.assertNotIn("synthetic-session", json.dumps(result))

    async def test_tools_duplicate_keys_oversize_and_bad_results_handoff(self):
        for raw in (b'{"toolCalls":[{"name":"bookAppointment"}]}', b'{"text":"one","text":"two"}', b'x' * 32769):
            async def stream():
                yield raw
            request = SimpleNamespace(headers={"content-type": "application/json"}, url=SimpleNamespace(query=""), stream=stream)
            with patch.object(consumer, "consult_async", new=AsyncMock()) as resolve:
                result = await public_webhook(request)
                self.assertEqual(result["lacs"]["outcome"], "BLOCKED")
                resolve.assert_not_awaited()

    async def test_public_voice_uses_public_resolution_and_json_sse_agree(self):
        harness = preview_tests.VoicePreviewTests()
        harness.setUp()
        resolve = AsyncMock(return_value=consumer.Decision("MATCH", suggestion=PUBLIC))
        app = LacsVoicePublic(enabled=True, key=KEY, resolver=resolve)
        body = {"model": app.model, "messages": [{"role": "user", "content": ROW["question"]}]}
        with patch.multiple(consumer, ENABLED=True, DELIVERY="approved-public"):
            sent = await harness.invoke(body, app=app, root="/vapi/lacs-public")
            self.assertEqual(json.loads(harness.body(sent))["choices"][0]["message"]["content"], PUBLIC["answer"])
            self.assertIn((b"x-lacs-preview", b"approved-public"), sent[0]["headers"])
            streamed = await harness.invoke(dict(body, stream=True), app=app, root="/vapi/lacs-public")
            chunks = [json.loads(line[6:]) for line in harness.body(streamed).decode().splitlines() if line.startswith("data: {")]
            self.assertEqual(''.join(c['choices'][0]['delta'].get('content', '') for c in chunks), PUBLIC['answer'])
            resolve.assert_awaited_with(ROW["question"], "voice-public", privileged=False)
            resolve.return_value = consumer.Decision("MATCH", suggestion=ANSWER)
            rejected = await harness.invoke(body, app=app, root="/vapi/lacs-public")
            self.assertEqual(json.loads(harness.body(rejected))["choices"][0]["message"]["content"], consumer.HANDOFF_TEXT)
            unauth = await harness.invoke(body, app=app, headers=[], root="/vapi/lacs-public")
            self.assertEqual(unauth[0]["status"], 401)
        with patch.object(consumer, "DELIVERY", "shadow"):
            self.assertEqual((await harness.invoke(body, app=app))[0]["status"], 404)
