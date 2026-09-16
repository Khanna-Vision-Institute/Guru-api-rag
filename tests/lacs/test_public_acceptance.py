import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from verify_lacs_public import verify, CheckFailed
from lacs_voice_preview import SOURCE_FILES, LacsVoicePublic
from test_public_delivery import PUBLIC


class PublicAcceptanceTests(unittest.TestCase):
    def call(self, base, path, header=None, body=None, accept="application/json"):
        if path.endswith("/status"):
            if header is None:
                return 401, "application/json", {}, b'{}'
            value = {"protocol": "lacs-voice-public-v1", "consumerEnabled": True, "deliveryMode": self.mode,
                     "sourceSha256": {n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in SOURCE_FILES}}
        elif path == "/vapi/webhook":
            value = {"lacs": dict(PUBLIC, outcome="MATCH"), "messages": [{"text": PUBLIC["answer"]}]}
            if "toolCalls" in body:
                value = {"lacs": {"outcome": "BLOCKED"}}
        elif path == "/guru/chat":
            value = {"answer": PUBLIC["answer"]}
        elif body.get("stream"):
            chunk = {"id": "synthetic", "model": LacsVoicePublic.model, "object": "chat.completion.chunk",
                     "choices": [{"delta": {"content": PUBLIC["answer"]}, "finish_reason": "stop"}]}
            return 200, "text/event-stream", {}, ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()
        else:
            value = {"choices": [{"message": {"content": self.voice_answer}}]}
        return 200, "application/json", {}, json.dumps(value).encode()

    def test_transport_pass_does_not_claim_real_call_or_all_personas(self):
        self.mode = "approved-public"
        self.voice_answer = PUBLIC["answer"]
        result = verify({"question": PUBLIC["question"], "publicEducationConfirmed": True},
                        "https://khannainstitute.com/api/guru", "synthetic-public-key-not-a-real-secret", self.call)
        self.assertTrue(result["publicHttpDeliveryVerified"])
        self.assertFalse(result["actualVapiCallVerified"])
        self.assertFalse(result["allPersonaConfigurationsVerified"])
        for secret in (PUBLIC["answer"], PUBLIC["question"], "synthetic-public-key-not-a-real-secret"):
            self.assertNotIn(secret, json.dumps(result))

    def test_shadow_deployment_or_voice_mismatch_cannot_pass(self):
        for mode, answer in (("shadow", PUBLIC["answer"]), ("approved-public", "Legacy synthetic answer")):
            self.mode, self.voice_answer = mode, answer
            with self.assertRaises(CheckFailed):
                verify({"question": PUBLIC["question"], "publicEducationConfirmed": True},
                       "https://khannainstitute.com/api/guru", "synthetic-public-key-not-a-real-secret", self.call)
