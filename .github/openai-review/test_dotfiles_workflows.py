import unittest
from pathlib import Path


class DotfilesWorkflowTests(unittest.TestCase):
    def test_workflow_preserves_reviewer_exit_status_and_gates_decline_notes(self):
        workflow = (
            Path(__file__).resolve().parent.parent / "workflows/openai-pr-review.yml"
        )
        raw = workflow.read_text(encoding="utf-8")
        job = raw.split("\n  review:\n", 1)[1].split("\n  comment:\n", 1)[0]
        self.assertNotIn("continue-on-error", job)
        self.assertIn("\n          python3 -B .github/openai-review/review.py\n", job)
        self.assertIn(
            "outputs.identity != '' && steps.review.outputs.decline_note == ''", job
        )
        self.assertIn("needs.review.outputs.decline_note != ''", raw)
        self.assertIn('if [[ "${revision}" == "open ${DECLINE_HEAD}" ]]; then', raw)
