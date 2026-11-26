import unittest
from unittest.mock import Mock

import triage


class TriageTests(unittest.TestCase):
    def setUp(self):
        self.candidates = [{"id": "F1"}, {"id": "F2"}, {"id": "F3"}]
        self.answers = [
            {"type": "predicate", "name": "F1_defect", "probability": 0.85},
            {"type": "score", "name": "F1_severity", "score": 1},
            {"type": "predicate", "name": "F2_defect", "probability": 0.84},
            {"type": "score", "name": "F2_severity", "score": 3},
            {"type": "predicate", "name": "F3_defect", "probability": 0.99},
            {"type": "score", "name": "F3_severity", "score": 0.99},
        ]

    def test_batches_predicate_and_score_and_filters_on_both(self):
        send = Mock(return_value={"answers": self.answers})
        self.assertEqual(
            triage.select(self.candidates, ["evidence"], "test", send), [{"id": "F1"}]
        )
        send.assert_called_once()
        self.assertEqual(send.call_args.args, (triage.ENDPOINT, "test"))
        body = send.call_args.kwargs["body"]
        self.assertEqual(body["model"], "gpt-6-luna")
        self.assertEqual(len(body["questions"]), 6)
        self.assertEqual(
            body["input"][1]["content"][0]["data"],
            {"patches": ["evidence"], "findings": self.candidates},
        )

    def test_refused_question_rejects_only_that_finding(self):
        self.answers[2] = {"type": "refusal", "name": "F2_defect"}
        send = Mock(return_value={"answers": self.answers})
        self.assertEqual(
            triage.select(self.candidates, [], "test", send), [{"id": "F1"}]
        )

    def test_malformed_responses_fail_closed(self):
        for answers in (
            self.answers[1:],
            [*reversed(self.answers)],
            [
                {"type": "predicate", "name": "F1_defect", "probability": True},
                *self.answers[1:],
            ],
        ):
            with self.subTest(answers=answers), self.assertRaises(triage.TriageError):
                triage.select(
                    self.candidates, [], "test", Mock(return_value={"answers": answers})
                )

    def test_empty_or_oversized_request_does_not_call_api(self):
        send = Mock()
        self.assertEqual(triage.select([], ["evidence"], "test", send), [])
        with self.assertRaises(triage.TriageError):
            triage.select(
                self.candidates, ["a" * triage.MAX_REQUEST_BYTES], "test", send
            )
        send.assert_not_called()
