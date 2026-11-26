# Source of truth: ray-manaloto/dotfiles-v2 .github/openai-review/; edit there and sync.
import unittest
from unittest.mock import Mock, patch

import triage


class TriageTests(unittest.TestCase):
    def setUp(self):
        self.candidates = [{"id": "F1"}, {"id": "F2"}, {"id": "F3"}]
        self.answers = [
            {"type": "predicate", "name": "F1_defect", "probability": 0.85},
            {"type": "score", "name": "F1_severity", "score": 0.0},
            {"type": "predicate", "name": "F2_defect", "probability": 0.84},
            {"type": "score", "name": "F2_severity", "score": 3},
            {"type": "predicate", "name": "F3_defect", "probability": 0.99},
            {"type": "score", "name": "F3_severity", "score": 1.0},
        ]

    def test_batches_predicate_and_score_and_filters_on_both(self):
        send = Mock(return_value={"answers": self.answers})
        self.assertEqual(
            triage.partition(self.candidates, ["evidence"], "test", send),
            ([{"id": "F3"}], [{"id": "F1"}]),
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
        for index in (0, 1):
            with self.subTest(question=self.answers[index]["name"]):
                answers = self.answers.copy()
                answers[index] = {"type": "refusal", "name": answers[index]["name"]}
                send = Mock(return_value={"answers": answers})
                self.assertEqual(
                    triage.partition(self.candidates, [], "test", send),
                    ([{"id": "F3"}], []),
                )

    def test_lowering_posting_floor_promotes_shadow(self):
        send = Mock(return_value={"answers": self.answers})
        with patch.object(triage, "MIN_SEVERITY", triage.SHADOW_MIN_SEVERITY):
            self.assertEqual(
                triage.partition(self.candidates, [], "test", send),
                ([{"id": "F1"}, {"id": "F3"}], []),
            )
            self.assertEqual(
                triage.select(self.candidates, [], "test", send),
                [{"id": "F1"}, {"id": "F3"}],
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
