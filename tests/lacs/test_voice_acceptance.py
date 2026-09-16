import hashlib
import json
import unittest

from scripts.verify_lacs_voice_preview import CheckFailed, MODEL, ROOT, SOURCE_FILES, request, stream_text, verify


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.answer = "Synthetic reviewed answer."
        self.hashes = {n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in SOURCE_FILES}
        self.status = {"protocol": "lacs-voice-staff-preview-v1", "sourceSha256": self.hashes,
                       "consumerEnabled": True, "deliveryMode": "shadow"}
        self.model_used = "lacs-approved-qa"
        self.voice_answer = self.answer

    def response(self, value):
        return 200, "application/json", {}, json.dumps(value).encode()

    def fake_call(self, base, path, header=None, body=None, accept="application/json"):
        self.calls.append((path, header, body))
        if path.endswith("/status"):
            return (401, "application/json", {}, b"{}") if header is None else self.response(self.status)
        if path == "/ask":
            return self.response({"answer": self.answer, "model_used": self.model_used})
        if body.get("stream"):
            chunk = {"id": "synthetic", "object": "chat.completion.chunk", "model": MODEL,
                     "choices": [{"delta": {"content": self.voice_answer}, "finish_reason": "stop"}]}
            return 200, "text/event-stream", {}, b"data: " + json.dumps(chunk).encode() + b"\n\ndata: [DONE]\n\n"
        return self.response({"choices": [{"message": {"content": self.voice_answer}}]})

    def run_verification(self):
        return verify({"question": "Synthetic general question?", "publicEducationConfirmed": True},
                      "https://khannainstitute.com/api/guru", "synthetic-admin", "synthetic-voice-key" * 3,
                      self.fake_call)

    def test_pass_proves_http_comparison_but_never_claims_real_vapi_or_public_delivery(self):
        result = self.run_verification()
        self.assertEqual(result["result"], "pass")
        self.assertFalse(result["actualVapiCallVerified"])
        self.assertFalse(result["publicDeliveryEnabled"])
        self.assertNotIn(self.answer, json.dumps(result))
        self.assertEqual(len(self.calls), 5)

    def test_old_sources_or_public_mode_block_acceptance(self):
        for bad in ({**self.status, "sourceSha256": {}}, {**self.status, "deliveryMode": "live"},
                    {**self.status, "consumerEnabled": False}):
            self.status = bad
            with self.assertRaises(CheckFailed):
                self.run_verification()

    def test_legacy_answer_is_not_mistaken_for_approved_delivery(self):
        self.model_used = "legacy"
        with self.assertRaisesRegex(CheckFailed, "text_not_from_approved_lacs"):
            self.run_verification()

    def test_rewritten_voice_answer_fails(self):
        self.voice_answer = self.answer + " Unapproved addition."
        with self.assertRaisesRegex(CheckFailed, "text_voice_mismatch"):
            self.run_verification()

    def test_incomplete_stream_fails(self):
        with self.assertRaisesRegex(CheckFailed, "stream_incomplete"):
            stream_text((200, "text/event-stream", {}, b"data: {}\n\n"))

    def test_untrusted_origins_and_private_case_are_refused_before_network(self):
        for base in ("http://khannainstitute.com", "https://evil.example", "https://user:pass@khannainstitute.com",
                     "https://khannainstitute.com/api/guru?secret=x", "https://khannainstitute.com:8443"):
            with self.assertRaises(CheckFailed):
                request(base, "/status")
        with self.assertRaises(CheckFailed):
            verify({"question": "x", "publicEducationConfirmed": False}, "", "", "", self.fake_call)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
