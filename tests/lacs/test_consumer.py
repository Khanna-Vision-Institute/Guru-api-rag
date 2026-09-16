"""No live credentials, network, model calls or application import."""
import asyncio
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock, patch

import lacs_consumer as consumer
from lacs_approved_qa import Unavailable, _NoRedirect
from test_client import ANSWER, ROW


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.multiple(consumer, ENABLED=True, DELIVERY="shadow"))
        self.client = Mock()
        self.client.suggest.return_value = ANSWER
        self.stack.enter_context(patch.object(consumer, "_get_client", return_value=self.client))

    def test_staff_exact_match_unchanged_and_no_legacy(self):
        decision = consumer.consult(ROW["question"], privileged=True)
        self.assertEqual(decision.outcome, "MATCH")
        self.assertEqual(decision.text, ANSWER["answer"])
        self.assertFalse(decision.allow_legacy)

    def test_successful_miss_allows_staff_legacy(self):
        self.client.suggest.return_value = None
        decision = consumer.consult("Synthetic miss?", privileged=True)
        self.assertEqual(decision.outcome, "NO_MATCH")
        self.assertTrue(decision.allow_legacy)

    def test_failures_do_not_authorize_fallback(self):
        for failure in [Unavailable("synthetic"), TimeoutError(), ValueError("secret-canary")]:
            self.client.suggest.side_effect = failure
            decision = consumer.consult(ROW["question"], privileged=True)
            self.assertEqual(decision.outcome, "BLOCKED")
            self.assertFalse(decision.allow_legacy)
            self.assertEqual(decision.text, consumer.HANDOFF_TEXT)
            self.assertIsNone(decision.suggestion)

    def test_shadow_preserves_legacy_but_exposes_no_answer(self):
        for result, expected in [(ANSWER, "MATCH"), (None, "NO_MATCH"), (Unavailable("synthetic"), "BLOCKED")]:
            self.client.suggest.side_effect = result if isinstance(result, Exception) else None
            self.client.suggest.return_value = result
            decision = consumer.consult(ROW["question"])
            self.assertEqual(decision.outcome, "SHADOW")
            self.assertEqual(decision.observed_outcome, expected)
            self.assertTrue(decision.allow_legacy)
            self.assertIsNone(decision.suggestion)

    def test_off_is_off_even_for_admin(self):
        for enabled, delivery in [(False, "shadow"), (True, "off")]:
            with patch.multiple(consumer, ENABLED=enabled, DELIVERY=delivery):
                self.assertEqual(consumer.consult(ROW["question"], privileged=True).outcome, "BYPASS")
        self.client.suggest.assert_not_called()

    def test_live_or_unknown_mode_cannot_activate_patient_delivery(self):
        for delivery in ("live", "LIVE-typo", ""):
            for privileged in (False, True):
                with patch.object(consumer, "DELIVERY", delivery):
                    decision = consumer.consult(ROW["question"], privileged=privileged)
                    self.assertEqual(decision.outcome, "BLOCKED")
                    self.assertFalse(decision.allow_legacy)
        self.client.suggest.assert_not_called()

    def test_initialization_failure_blocks(self):
        with patch.object(consumer, "_get_client", side_effect=ValueError("secret-canary")):
            self.assertFalse(consumer.consult(ROW["question"], privileged=True).allow_legacy)

    def test_logs_only_fixed_channel_and_outcome(self):
        self.client.suggest.side_effect = Unavailable("SECRET-canary")
        with self.assertLogs(consumer.log, level="INFO") as captured:
            consumer.consult("PATIENT-canary", "CHANNEL-canary", privileged=True)
        logged = "\n".join(captured.output)
        self.assertIn("channel=unknown outcome=BLOCKED", logged)
        for forbidden in ("PATIENT-canary", "SECRET-canary", "CHANNEL-canary"):
            self.assertNotIn(forbidden, logged)

    def test_admin_key_required_and_safe(self):
        with patch.object(consumer, "ADMIN_KEY", "synthetic-admin"):
            self.assertTrue(consumer.is_admin(SimpleNamespace(headers={"x-guru-key": "synthetic-admin"})))
            self.assertFalse(consumer.is_admin(SimpleNamespace(headers={"x-guru-key": "wrong"})))
            self.assertFalse(consumer.is_admin(None))
        with patch.object(consumer, "ADMIN_KEY", ""):
            self.assertFalse(consumer.is_admin(SimpleNamespace(headers={"x-guru-key": ""})))

    def test_timed_out_workers_remain_bounded_without_queue(self):
        release = threading.Event()
        executor = ThreadPoolExecutor(max_workers=2)
        def hold(_):
            release.wait(2)
            return ANSWER
        self.client.suggest.side_effect = hold
        try:
            with patch.multiple(consumer, _executor=executor, _slots=threading.BoundedSemaphore(2),
                                OVERALL_DEADLINE_S=0.02):
                for _ in range(8):
                    self.assertEqual(consumer.consult(ROW["question"], privileged=True).outcome, "BLOCKED")
                self.assertEqual(self.client.suggest.call_count, 2)
                release.set()
                executor.shutdown(wait=True)
                self.assertTrue(consumer._slots.acquire(blocking=False))
                self.assertTrue(consumer._slots.acquire(blocking=False))
                self.assertFalse(consumer._slots.acquire(blocking=False))
                consumer._slots.release()
                consumer._slots.release()
        finally:
            release.set()
            executor.shutdown(wait=True)

    def test_async_outcomes_match_sync_without_extra_executor(self):
        async def scenario():
            match = await consumer.consult_async(ROW["question"], privileged=True)
            self.assertEqual(match.outcome, "MATCH")
            self.client.suggest.return_value = None
            miss = await consumer.consult_async(ROW["question"], privileged=True)
            self.assertEqual(miss.outcome, "NO_MATCH")
            self.client.suggest.side_effect = Unavailable("synthetic")
            blocked = await consumer.consult_async(ROW["question"], privileged=True)
            self.assertFalse(blocked.allow_legacy)
        asyncio.run(scenario())

    def test_async_deadline_does_not_block_event_loop(self):
        release = threading.Event()
        executor = ThreadPoolExecutor(max_workers=2)
        def hold(_):
            release.wait(2)
            return ANSWER
        self.client.suggest.side_effect = hold
        try:
            with patch.multiple(consumer, _executor=executor, _slots=threading.BoundedSemaphore(1),
                                OVERALL_DEADLINE_S=0.02):
                # One occupied slot means no free permit after the deadline.
                async def bounded_scenario():
                    task = asyncio.create_task(consumer.consult_async(ROW["question"], privileged=True))
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    self.assertEqual((await task).outcome, "BLOCKED")
                    self.assertFalse(consumer._slots.acquire(blocking=False))
                asyncio.run(bounded_scenario())
        finally:
            release.set()
            executor.shutdown(wait=True)


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.multiple(
            consumer, TOKEN_URL="https://identity.example.invalid/token", CLIENT_ID="synthetic-client",
            CLIENT_SECRET="synthetic-secret", _token_cache={"value": None, "exp": 0.0}))
        self.open = self.stack.enter_context(patch.object(consumer._no_proxy_opener, "open"))

    def response(self, payload, content_type="application/json", status=200):
        response = Mock()
        response.status = status
        response.headers = Message()
        response.headers["Content-Type"] = content_type
        response.read.return_value = payload
        self.open.return_value.__enter__.return_value = response
        return response

    def test_valid_token_cached_and_read_bounded(self):
        response = self.response(json.dumps({"access_token": "synthetic-token", "expires_in": 300}).encode())
        self.assertEqual(consumer._token_provider(), "synthetic-token")
        self.assertEqual(consumer._token_provider(), "synthetic-token")
        self.open.assert_called_once()
        response.read.assert_called_once_with(65537)

    def test_expired_token_refreshed(self):
        consumer._token_cache.update(value="synthetic-expired", exp=time.monotonic() - 1)
        self.response(json.dumps({"access_token": "synthetic-new", "expires_in": 300}).encode())
        self.assertEqual(consumer._token_provider(), "synthetic-new")

    def test_invalid_json_and_tokens_rejected(self):
        payloads = [b"[]", b"null", b"not-json", b"x" * 65537]
        for ttl in (True, -1, 0, 3601, float("inf"), float("nan"), "300"):
            payloads.append(json.dumps({"access_token": "synthetic-token", "expires_in": ttl}).encode())
        for token in ("", "x\ny", "x" * 16385, None):
            payloads.append(json.dumps({"access_token": token, "expires_in": 300}).encode())
        for payload in payloads:
            self.response(payload)
            with self.assertRaisesRegex(Unavailable, "^LACS token unavailable$"):
                consumer._token_provider()

    def test_bad_status_or_content_type_rejected(self):
        for status, content_type in [(302, "application/json"), (401, "application/json"), (200, "text/html")]:
            self.response(b"{}", content_type, status)
            with self.assertRaises(Unavailable):
                consumer._token_provider()

    def test_token_endpoint_must_be_fixed_trusted_https(self):
        for url in ("http://identity.example.invalid/token", "https://user:pass@identity.example.invalid/token",
                    "https://identity.example.invalid/token?redirect=x", "https://identity.example.invalid:444/token",
                    "https://identity.example.invalid/token#x", " https://identity.example.invalid/token"):
            with patch.object(consumer, "TOKEN_URL", url):
                with self.assertRaises(Unavailable):
                    consumer._token_provider()
        self.open.assert_not_called()

    def test_redirects_rejected_and_errors_redacted(self):
        self.assertTrue(any(isinstance(handler, _NoRedirect) for handler in consumer._no_proxy_opener.handlers))
        self.open.side_effect = RuntimeError("SECRET-canary")
        with self.assertRaisesRegex(Unavailable, "^LACS token unavailable$"):
            consumer._token_provider()


if __name__ == "__main__":
    unittest.main()
