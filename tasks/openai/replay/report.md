# Review replay report

Batch status is **completed**. Collected **161** merged PRs. dotfiles-v2 32, knowledge-base-v2 129.
Production patch checks admitted **114** and declined **47**.
Successfully parsed **114** Batch reviews.

## Spend

- Pre-submit upper estimate $13.194583, comprising Batch $5.818327 and Decisions $7.376256.
- Metered usage at published rates $0.559018, comprising Batch $0.497461 and Decisions $0.061557. Unreconciled reservation $0.000000.
- Metered cost per parsed PR $0.004904.
- Batch output usage records 313,012 input tokens (including 0 cached) and 22,682 output tokens.
- Decisions usage records 523,418 input tokens (including 8,404 cached reads and 394,387 cache writes).

## Declines and coverage

- 45 PRs — The PR exceeds the patch size limit.
- 2 PRs — The PR exceeds the reviewable file limit.
- 34 skipped files — missing-patch
- 31 skipped files — oversized
- 17 skipped files — generated-or-lockfile
- 6 skipped files — sensitive-text
- changed line candidates — 138
- graded with follow up sources — 136
- missing or unmetered batch — 0
- parsed — 114
- production selected — 2
- raw candidates — 148
- raw grade defect — 70
- raw grade incorrect — 4
- raw grade insufficient-evidence — 74

## Triage thresholds

Resolved precision excludes inconclusive labels. The precision lower bound counts every inconclusive selection as a false positive. Raw recall uses all labeled defects in the raw candidate pool. Changed-line recall uses only labeled defects on lines that can reach production triage. Candidates with unavailable triage are unselected.

| Probability | Severity | Selected | True | False | Unknown | Resolved precision | Precision lower bound | Raw recall | Changed-line recall |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.70 | 0.0 | 114 | 64 | 3 | 47 | 95.5% | 56.1% | 91.4% | 94.1% |
| 0.70 | 1.0 | 2 | 0 | 0 | 2 | — | 0.0% | 0.0% | 0.0% |
| 0.70 | 2.0 | 0 | 0 | 0 | 0 | — | — | 0.0% | 0.0% |
| 0.80 | 0.0 | 112 | 64 | 3 | 45 | 95.5% | 57.1% | 91.4% | 94.1% |
| 0.80 | 1.0 | 2 | 0 | 0 | 2 | — | 0.0% | 0.0% | 0.0% |
| 0.80 | 2.0 | 0 | 0 | 0 | 0 | — | — | 0.0% | 0.0% |
| 0.85 | 0.0 | 105 | 59 | 2 | 44 | 96.7% | 56.2% | 84.3% | 86.8% |
| 0.85 | 1.0 | 2 | 0 | 0 | 2 | — | 0.0% | 0.0% | 0.0% |
| 0.85 | 2.0 | 0 | 0 | 0 | 0 | — | — | 0.0% | 0.0% |
| 0.90 | 0.0 | 95 | 54 | 1 | 40 | 98.2% | 56.8% | 77.1% | 79.4% |
| 0.90 | 1.0 | 2 | 0 | 0 | 2 | — | 0.0% | 0.0% | 0.0% |
| 0.90 | 2.0 | 0 | 0 | 0 | 0 | — | — | 0.0% | 0.0% |
| 0.95 | 0.0 | 51 | 32 | 1 | 18 | 97.0% | 62.7% | 45.7% | 47.1% |
| 0.95 | 1.0 | 1 | 0 | 0 | 1 | — | 0.0% | 0.0% | 0.0% |
| 0.95 | 2.0 | 0 | 0 | 0 | 0 | — | — | 0.0% | 0.0% |

The production thresholds selected 2 candidates. The grader labeled 2 inconclusive and recovered 0 of 70 labeled defects across all raw candidates. 68 labeled defects were on changed lines.

## False-positive labels in the raw candidate pool

- incorrect — 4

For the review prompt, require direct patch evidence for each factual claim and omit candidates that depend on missing implementation.

## Limitations

The grader is a model, and shares its model with production triage. Agreement can reflect shared blind spots. Follow-up source and patches are byte-bounded excerpts. A later edit is corroboration only. A lack of a later edit is inconclusive. Path renames, squashes, and unavailable context can hide a real fix. The sample includes only the two chosen repositories and only findings returned on their admitted patches.

## Independent re-grade

GPT-6 Sol graded all 105 candidates selected at probability 0.85 and severity 0.0, plus a simple random sample of 30 of the 43 rejects. Sampling sorted internal IDs used seed `20261126`. Sol received the same excerpts and rubric. Luna labels and triage scores were withheld. 135 of 135 Responses requests completed with `store: false`.

| Grader | Selected defects / nondefects / inconclusive | Sampled reject defects / nondefects / inconclusive | Resolved precision | Precision lower bound | Estimated raw recall |
| --- | ---: | ---: | ---: | ---: | ---: |
| GPT-6 Luna | 59 / 2 / 44 | 7 / 2 / 21 | 96.7% | 56.2% | 85.5% |
| GPT-6 Sol | 73 / 6 / 26 | 10 / 6 / 14 | 92.4% | 69.5% | 83.6% |

Each sampled reject represents 43/30 rejects when estimating raw recall.
Luna's full-cohort recall was 84.3% across all raw candidates and 86.8% among changed-line candidates. Under Sol, production selected 2 candidates (defects 1, nondefects 0, inconclusive 1). Estimated raw recall was 1.1%.

The graders assigned the same six-way label to 92 of 135 candidates (68.1%), including 36 jointly inconclusive cases. They agreed on defect versus nondefect for 57 of the 66 cases both resolved (86.4%). Sol called 24 of Luna's inconclusive cases defects.

The follow-up upper estimate was **$8.570862** against a **$10** added cap. Metered usage cost **$0.793424** at standard rates. The responses recorded 210,207 input tokens, including 0 cached reads and 183,759 30-minute cache writes, and 28,113 output tokens. [OpenAI API pricing](https://developers.openai.com/api/docs/pricing)

### Revised recommendation

Keep `MIN_PROBABILITY = 0.85` and lower `MIN_SEVERITY` to `0.0` for the next calibration rollout. Sol's estimated raw recall rises from 1.1% to 83.6%. The lower floor recovered 72 additional Sol-labeled defects and selected 6 additional labeled nondefects. Drop the zero-additional-false-positive rule. Selection grew from 2 to 105. Validate that volume in shadow mode before enabling automatic posting.

<!-- BEGIN REPLAY SPOT CHECKS -->
### Spot checks

Ten pairs were sampled from the re-graded candidates in the public repository using seed `20261127`. Finding summaries are paraphrased.

| Path | Finding summary | Luna | Sol |
| --- | --- | --- | --- |
| `unix/home/.config/mise/tasks/bootstrap/skills` | A working AgentsView installation elsewhere on PATH can be skipped by the fixed-path guard. | Defect | Defect |
| `hk.pkl` | A sufficiently large pre-push payload can exceed Linux's maximum length for a single argument. | Inconclusive | Defect |
| `tasks/pr/land` | The fast-forward pull permits an already-ahead local main branch to retain unpushed commits. | Defect | Defect |
| `tasks/py/doctor_checks.py` | A non-activated shell can incur an extra mise subprocess when its result is unusable. | Inconclusive | Inconclusive |
| `unix/home/.config/mise/tasks/repowise/init` | A pre-existing child index can falsely trigger parent guard removal. | Defect | Defect |
| `.github/workflows/claude-pr-review.yml` | The patch excerpt provides insufficient evidence that the workflow's shared reviewer exists on the target branch. | Inconclusive | Duplicate |
| `hk.pkl` | Keeping the first combined output line can hide warnings from successful mise checks. | Defect | Defect |
| `tasks/py/doctor_checks.py` | The shell guard misses the blocked variable when it appears later in a multi-variable export. | Inconclusive | Defect |
| `tasks/py/doctor_checks.py` | The shell guard can treat a local assignment as an export. | Defect | Defect |
| `unix/home/.config/mise/lib/api_runner/runner.py` | An unexpected stream event subtype can carry arbitrary text into a loggable diagnostic. | Defect | Defect |
<!-- END REPLAY SPOT CHECKS -->
