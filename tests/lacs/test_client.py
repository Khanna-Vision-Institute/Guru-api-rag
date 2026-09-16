"""Synthetic contract tests; never contact LACS or load the Guru application."""
import unittest
import urllib.error
from unittest.mock import patch, Mock

from lacs_approved_qa import ApprovedQaClient, Unavailable, SCHEMA, _NoRedirect, _question_hash

REF = {"documentId": "clinical-qa:" + "a" * 64, "version": 2, "integrityHash": "b" * 64}
ROW = dict(REF, question="Synthetic reviewed question?", keywords=["synthetic"], procedureTags=[])
PAGE = {"schemaVersion": SCHEMA, "requiresHumanReview": True, "items": [ROW], "nextCursor": None}
ANSWER = dict(REF, schemaVersion=SCHEMA, requiresHumanReview=True, question=ROW["question"],
              answer="  Synthetic reviewed wording.\n", supportingUrls=["https://clinical.example.invalid/qa"])


class ClientTests(unittest.TestCase):
    def client(self):
        client = ApprovedQaClient("https://lacs.example.invalid", lambda: "synthetic-test-only")
        client._coverage = Mock(return_value={"revision": "synthetic", "questionHashes": [_question_hash(ROW["question"])]})
        return client

    def test_exact_match_sends_only_reference_then_returns_reviewed_suggestion(self):
        client = self.client()
        with patch.object(client, "_request", side_effect=[PAGE, ANSWER]) as request:
            self.assertEqual(client.suggest(ROW["question"]), ANSWER)
            self.assertEqual(request.call_args_list[1].args[1], REF)
            self.assertNotIn(ROW["question"], str(request.call_args_list))

    def test_normalization_is_only_case_whitespace_question_mark(self):
        client = self.client()
        with patch.object(client, "_request", side_effect=[PAGE, ANSWER]):
            self.assertEqual(client.suggest("  SYNTHETIC   reviewed question  "), ANSWER)
        with patch.object(client, "_request", return_value=PAGE):
            self.assertIsNone(client.suggest("Synthetic unreviewed variation?"))

    def test_no_match_only_after_all_pages(self):
        client = self.client()
        page2 = dict(PAGE, items=[dict(ROW, documentId="clinical-qa:" + "c" * 64)])
        with patch.object(client, "_request", side_effect=[dict(PAGE, nextCursor=REF["documentId"]), page2]) as request:
            self.assertIsNone(client.suggest("Another synthetic question?"))
            self.assertEqual(request.call_count, 2)

    def test_ambiguous_is_blocked_not_no_match(self):
        client = self.client()
        ambiguous = dict(PAGE, items=[ROW, dict(ROW, documentId="clinical-qa:" + "c" * 64)])
        with patch.object(client, "_request", return_value=ambiguous) as request:
            with self.assertRaises(Unavailable):
                client.suggest(ROW["question"])
            self.assertEqual(request.call_count, 1)

    def test_invalid_input_is_not_a_miss(self):
        for question in ("", " ", None, "x" * 501, 17):
            with self.subTest(question_type=type(question).__name__):
                client = self.client()
                with patch.object(client, "_request") as request:
                    with self.assertRaises(Unavailable):
                        client.suggest(question)
                    request.assert_not_called()

    def test_retirement_between_catalog_and_resolution_blocks(self):
        client = self.client()
        with patch.object(client, "_request", side_effect=[PAGE, Unavailable("Not current")]):
            with self.assertRaises(Unavailable):
                client.suggest(ROW["question"])

    def test_retirement_before_catalog_is_blocked_by_coverage(self):
        # Historical coverage prevents a withdrawn answer becoming a legacy miss.
        client = self.client()
        with patch.object(client, "_request", return_value=dict(PAGE, items=[])):
            with self.assertRaises(Unavailable):
                client.suggest(ROW["question"])

    def test_revision_or_unreviewed_response_rejected(self):
        for changes in [{"version": 3}, {"question": "Changed question"}, {"requiresHumanReview": False},
                        {"answer": ""}, {"integrityHash": "d" * 64}, {"approvedBy": "synthetic"}]:
            client = self.client()
            with patch.object(client, "_request", side_effect=[PAGE, dict(ANSWER, **changes)]):
                with self.assertRaises(Unavailable):
                    client.suggest(ROW["question"])

    def test_refetches_for_each_suggestion(self):
        client = self.client()
        with patch.object(client, "_request", side_effect=[PAGE, ANSWER, dict(PAGE, items=[])]) as request:
            self.assertEqual(client.suggest(ROW["question"]), ANSWER)
            with self.assertRaises(Unavailable):
                client.suggest(ROW["question"])
            self.assertEqual(request.call_count, 3)

    def test_invalid_origin_and_redirect(self):
        for origin in ["http://lacs.example.invalid", "https://user:pass@lacs.example.invalid",
                       "https://lacs.example.invalid/path", "https://lacs.example.invalid?query=x"]:
            with self.assertRaises(ValueError):
                ApprovedQaClient(origin, lambda: "synthetic")
        with self.assertRaises(Unavailable):
            _NoRedirect().redirect_request(None, None, 302, None, None, "https://other.example.invalid")

    def test_pagination_errors_never_become_misses(self):
        for page in [dict(PAGE, nextCursor="unsafe"), dict(PAGE, items=[ROW, ROW]),
                     dict(PAGE, items=[], nextCursor=REF["documentId"])]:
            client = self.client()
            with patch.object(client, "_request", return_value=page):
                with self.assertRaises(Unavailable):
                    client.suggest("No exact match?")

    def test_page_limit_is_not_a_complete_miss(self):
        client = self.client()
        pages = []
        for i in range(20):
            row = dict(ROW, documentId="clinical-qa:" + format(i, "064x"))
            pages.append(dict(PAGE, items=[row], nextCursor=row["documentId"]))
        with patch.object(client, "_request", side_effect=pages):
            with self.assertRaises(Unavailable):
                client.suggest("Another synthetic question?")

    def test_transport_error_statuses_redacted_and_not_no_match(self):
        for status in (401, 403, 404, 409, 429, 500):
            client = self.client()
            failure = urllib.error.HTTPError("https://lacs.example.invalid", status, "secret-canary", {}, None)
            with patch.object(client._opener, "open", side_effect=failure):
                with self.assertRaisesRegex(Unavailable, "^Approved knowledge unavailable$"):
                    client.suggest(ROW["question"])


if __name__ == "__main__":
    unittest.main()
