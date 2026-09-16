"""Execute the three real handler functions with synthetic dependencies.

AST loading deliberately avoids importing main.py: its module-level clients and
the repository's older root test scripts can contact live services. These are
routing regression tests, not an HTTP/authentication or deployment smoke test.
"""
import ast
import asyncio
import json
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import lacs_consumer as consumer
from test_client import ANSWER, ROW

ROOT = Path(__file__).resolve().parents[2]


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.search = Mock(return_value=[{"text": "Synthetic legacy context"}])
        self.generate = Mock(return_value=("Synthetic legacy answer", "synthetic-model"))
        self.facade = SimpleNamespace(consult=Mock(), consult_async=AsyncMock(),
                                      is_admin=Mock(return_value=True), public_mode=Mock(return_value=False),
                                      blocked_decision=consumer.blocked_decision)
        self.namespace = {
            "lacs_consumer": self.facade, "datetime": datetime, "json": json, "print": Mock(),
            "AskRequest": object, "VapiChatRequest": object, "Request": object,
            "public_webhook": AsyncMock(return_value={"synthetic": True}), "JSONResponse": SimpleNamespace,
            "AskResponse": SimpleNamespace, "VapiChatResponse": SimpleNamespace,
            "search_opensearch": self.search, "boost_rag_hits": lambda q, hits: hits,
            "generate_answer_with_fallback": self.generate,
            "sanitize_guru_answer": lambda text, query: text, "sanitize_phone_in_response": lambda text: text,
            "log_interaction": Mock(), "check_rate_limit": lambda ip: None,
            "sanitize_input": lambda text: (text, False), "booking_sessions": {}, "ecosystem_sessions": {},
            "detect_booking_intent": lambda text: False, "os": SimpleNamespace(getenv=lambda key: "0"),
        }
        parsed = ast.parse((ROOT / "main.py").read_text())
        handlers = [node for node in parsed.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name in ("ask_guru", "guru_chat", "vapi_webhook")]
        self.assertEqual(len(handlers), 3)
        for handler in handlers:
            handler.decorator_list = []
        exec(compile(ast.Module(body=handlers, type_ignores=[]), "main.py", "exec"), self.namespace)
        self.request = SimpleNamespace(query=ROW["question"], top_k=5, session_id="synthetic",
                                       headers={}, client=SimpleNamespace(host="127.0.0.1"),
                                       json=AsyncMock(return_value={"text": ROW["question"], "session_id": "synthetic"}))

    def invoke(self, name):
        if name == "ask_guru":
            return self.namespace[name](self.request, self.request).answer
        if name == "guru_chat":
            return self.namespace[name](self.request).answer
        self.webhook_result = asyncio.run(self.namespace[name](self.request))
        return self.webhook_result["messages"][0]["text"]

    def set_decision(self, decision):
        self.facade.consult.return_value = decision
        self.facade.consult_async.return_value = decision

    def test_all_routes_preserve_match_without_search_or_generation(self):
        self.set_decision(consumer.Decision("MATCH", suggestion=ANSWER))
        for name in ("ask_guru", "guru_chat", "vapi_webhook"):
            self.assertEqual(self.invoke(name), ANSWER["answer"])
        self.search.assert_not_called()
        self.generate.assert_not_called()
        self.assertEqual(self.webhook_result["lacs"]["integrityHash"], ANSWER["integrityHash"])

    def test_all_routes_block_without_legacy_search_or_generation(self):
        self.set_decision(consumer.blocked_decision())
        for name in ("ask_guru", "guru_chat", "vapi_webhook"):
            self.assertEqual(self.invoke(name), consumer.HANDOFF_TEXT)
        self.search.assert_not_called()
        self.generate.assert_not_called()
        self.assertNotIn("documentId", self.webhook_result["lacs"])

    def test_no_match_shadow_and_bypass_reach_legacy(self):
        for outcome in ("NO_MATCH", "SHADOW", "BYPASS"):
            self.set_decision(consumer.Decision(outcome))
            for name in ("ask_guru", "guru_chat", "vapi_webhook"):
                self.assertEqual(self.invoke(name), "Synthetic legacy answer")
        self.assertEqual(self.search.call_count, 9)
        self.assertEqual(self.generate.call_count, 9)

    def test_unexpected_consumer_exceptions_block_in_all_routes(self):
        self.facade.consult.side_effect = RuntimeError("SECRET-canary")
        self.facade.consult_async.side_effect = RuntimeError("SECRET-canary")
        for name in ("ask_guru", "guru_chat", "vapi_webhook"):
            self.assertEqual(self.invoke(name), consumer.HANDOFF_TEXT)
        self.search.assert_not_called()
        self.generate.assert_not_called()

    def test_public_webhook_intercepts_before_legacy_body_logging_and_tools(self):
        self.facade.public_mode.return_value = True
        result = asyncio.run(self.namespace["vapi_webhook"](self.request))
        self.assertEqual(result.content, {"synthetic": True})
        self.namespace["public_webhook"].assert_awaited_once_with(self.request, rate_limit=self.namespace["check_rate_limit"])
        self.request.json.assert_not_awaited()
        self.namespace["print"].assert_not_called()
        self.search.assert_not_called()
        self.generate.assert_not_called()

    def test_only_ask_uses_admin_staff_preview(self):
        self.set_decision(consumer.blocked_decision())
        self.invoke("ask_guru")
        self.facade.consult.assert_called_with(ROW["question"], "ask", privileged=True)
        self.invoke("guru_chat")
        self.facade.consult.assert_called_with(ROW["question"], "chat")
        self.invoke("vapi_webhook")
        self.facade.consult_async.assert_awaited_once_with(ROW["question"], "webhook")


if __name__ == "__main__":
    unittest.main()

