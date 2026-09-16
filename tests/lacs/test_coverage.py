import hashlib
import json
import unittest
from unittest.mock import patch
from lacs_approved_qa import ApprovedQaClient, Unavailable, SCHEMA, COVERAGE_SCHEMA, _question_hash

QUESTION = "What does synthetic EVO ICL follow-up involve?"
REF = {"documentId": "clinical-qa:" + "a" * 64, "version": 2, "integrityHash": "b" * 64}
ROW = dict(REF, question=QUESTION, keywords=[], procedureTags=["icl"])
PAGE = {"schemaVersion": SCHEMA, "requiresHumanReview": True, "items": [], "nextCursor": None}
ANSWER = dict(REF, schemaVersion=SCHEMA, requiresHumanReview=True, question=QUESTION,
              answer="Synthetic reviewed wording.", supportingUrls=["https://clinical.example.invalid/qa"])


def coverage(questions=()):
    hashes = sorted(set(_question_hash(q) for q in questions))
    revision = hashlib.sha256((COVERAGE_SCHEMA + "\0" + "\n".join(hashes)).encode()).hexdigest()
    return dict(schemaVersion=COVERAGE_SCHEMA, requiresHumanReview=True, revision=revision, questionHashes=hashes)


class CoverageTests(unittest.TestCase):
    def client(self):
        return ApprovedQaClient("https://lacs.example.invalid", lambda: "synthetic-token")

    def test_retired_expired_or_renamed_question_blocks_before_legacy(self):
        for question in (QUESTION, QUESTION.upper(), "  " + QUESTION + "  "):
            client = self.client()
            snapshot = coverage([QUESTION])
            with patch.object(client, "_request", side_effect=[snapshot, PAGE, snapshot]) as request:
                with self.assertRaisesRegex(Unavailable, "Previously covered"):
                    client.suggest(question)
                self.assertEqual(request.call_count, 3)
                self.assertNotIn(question, str(request.call_args_list))

    def test_never_approved_question_can_be_a_genuine_miss(self):
        client = self.client()
        snapshot = coverage([QUESTION])
        with patch.object(client, "_request", side_effect=[snapshot, PAGE, snapshot]):
            self.assertIsNone(client.suggest("What does synthetic LASIK testing involve?"))

    def test_new_approval_during_lookup_is_not_a_miss(self):
        client = self.client()
        with patch.object(client, "_request", side_effect=[coverage(), PAGE, coverage([QUESTION])]):
            with self.assertRaisesRegex(Unavailable, "Coverage changed"):
                client.suggest(QUESTION)

    def test_current_match_still_freshly_resolves(self):
        client = self.client()
        with patch.object(client, "_request", side_effect=[coverage([QUESTION]), dict(PAGE, items=[ROW]), ANSWER]) as request:
            self.assertEqual(client.suggest(QUESTION), ANSWER)
            self.assertEqual(request.call_args_list[-1].args[1], REF)

    def test_missing_or_failed_coverage_never_downgrades_to_v1(self):
        for stage in ("before", "after"):
            client = self.client()
            responses = [Unavailable("synthetic unavailable")] if stage == "before" else [coverage(), PAGE, Unavailable("synthetic unavailable")]
            with patch.object(client, "_request", side_effect=responses):
                with self.assertRaises(Unavailable):
                    client.suggest(QUESTION)

    def test_partial_unsorted_duplicate_or_tampered_coverage_rejected(self):
        valid = coverage([QUESTION, "What does synthetic LASIK testing involve?"])
        bads = [
            dict(valid, questionHashes=valid["questionHashes"][:1]),
            dict(valid, questionHashes=list(reversed(valid["questionHashes"]))),
            dict(valid, questionHashes=[valid["questionHashes"][0]] * 2),
            dict(valid, questionHashes=["x" * 64]),
            dict(valid, questionHashes=[1]),
            dict(valid, questionHashes=["a" * 64] * 5001),
            dict(valid, revision="a" * 64),
            dict(valid, schemaVersion=SCHEMA),
            dict(valid, requiresHumanReview=False),
            dict(valid, nextCursor="partial"),
            {},
        ]
        for value in bads:
            with self.subTest(keys=list(value)):
                client = self.client()
                with patch.object(client, "_request", return_value=value):
                    with self.assertRaises(Unavailable):
                        client.suggest(QUESTION)

    def test_second_snapshot_is_validated_too(self):
        client = self.client()
        with patch.object(client, "_request", side_effect=[coverage(), PAGE, dict(coverage(), revision="invalid")]):
            with self.assertRaises(Unavailable):
                client.suggest(QUESTION)

    def test_hash_is_local_and_exact_not_semantic(self):
        self.assertEqual(_question_hash(QUESTION), _question_hash(QUESTION.upper()))
        self.assertNotEqual(_question_hash("Straße?"), _question_hash("STRASSE?"))
        self.assertNotEqual(_question_hash(QUESTION), _question_hash(QUESTION.replace("follow-up", "followup")))


if __name__ == "__main__":
    unittest.main()

