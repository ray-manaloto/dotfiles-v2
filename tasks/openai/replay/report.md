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

Precision excludes insufficient-evidence labels. Recall measures recovery of labeled defects in the raw candidate pool only. The grid excludes PRs with unavailable production triage.

| Probability | Severity | Selected | True | False | Unknown | Precision | Recall |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.70 | 0.0 | 114 | 64 | 3 | 47 | 95.5% | 91.4% |
| 0.70 | 1.0 | 2 | 0 | 0 | 2 | — | 0.0% |
| 0.70 | 2.0 | 0 | 0 | 0 | 0 | — | 0.0% |
| 0.80 | 0.0 | 112 | 64 | 3 | 45 | 95.5% | 91.4% |
| 0.80 | 1.0 | 2 | 0 | 0 | 2 | — | 0.0% |
| 0.80 | 2.0 | 0 | 0 | 0 | 0 | — | 0.0% |
| 0.85 | 0.0 | 105 | 59 | 2 | 44 | 96.7% | 84.3% |
| 0.85 | 1.0 | 2 | 0 | 0 | 2 | — | 0.0% |
| 0.85 | 2.0 | 0 | 0 | 0 | 0 | — | 0.0% |
| 0.90 | 0.0 | 95 | 54 | 1 | 40 | 98.2% | 77.1% |
| 0.90 | 1.0 | 2 | 0 | 0 | 2 | — | 0.0% |
| 0.90 | 2.0 | 0 | 0 | 0 | 0 | — | 0.0% |
| 0.95 | 0.0 | 51 | 32 | 1 | 18 | 97.0% | 45.7% |
| 0.95 | 1.0 | 1 | 0 | 0 | 1 | — | 0.0% |
| 0.95 | 2.0 | 0 | 0 | 0 | 0 | — | 0.0% |

The production thresholds selected 2 candidates. The grader labeled 2 inconclusive and recovered 0 of the 70 labeled defects in the scored pool.

## Recurring false positives

- incorrect — 4

For the review prompt, require direct patch evidence for each factual claim and omit candidates that depend on missing implementation.

## Limitations

The grader is a model, and shares its model with production triage. Agreement can reflect shared blind spots. Follow-up source and patches are byte-bounded excerpts. A later edit is corroboration only. A lack of a later edit is inconclusive. Path renames, squashes, and unavailable context can hide a real fix. The sample includes only the two chosen repositories and only findings returned on their admitted patches.

## Independent re-grade

GPT-6 Sol independently graded all 105 candidates selected at probability 0.85 and severity 0.0. It also graded a simple random sample of 30 of the 43 rejects. The sample used Python's seeded random sampling after sorting internal candidate IDs, with seed `20261126`. The grader received the same excerpts and verdict rubric, with Luna labels and triage scores withheld. All 135 `/v1/responses` requests completed with `store: false`.

| Grader | Selected defects / nondefects / inconclusive | Sampled reject defects / nondefects / inconclusive | Resolved precision | Estimated candidate recall |
| --- | ---: | ---: | ---: | ---: |
| GPT-6 Luna | 59 / 2 / 44 | 7 / 2 / 21 | 96.7% | 85.5% |
| GPT-6 Sol | 73 / 6 / 26 | 10 / 6 / 14 | 92.4% | 83.6% |

Precision uses selected candidates with a conclusive verdict. Recall uses observed selected defects plus an estimate of rejected defects, weighting each sampled reject by 43/30. Luna's full-cohort recall was 84.3%. Under Sol, the current 0.85/1.0 thresholds selected one defect and one inconclusive candidate, for 1.1% estimated recall and precision based on just one resolved verdict. These are model estimates within the raw candidate pool. They exclude inconclusive verdicts from the defect counts.

The graders assigned the same six-way label to 92 of 135 candidates (68.1%). That includes 36 jointly inconclusive labels. They agreed on defect versus nondefect for 57 of the 66 cases both resolved (86.4%). Sol called 24 of Luna's inconclusive cases defects. Shared evidence limits both graders.

The follow-up upper estimate was **$8.570862** against a **$10** added cap. Metered usage cost **$0.793424** at standard rates. The 135 responses used 210,207 input tokens, including 183,759 30-minute cache writes, and 28,113 output tokens. [OpenAI API pricing](https://developers.openai.com/api/docs/pricing?offer=add-linebreak)

### Revised recommendation

Keep `MIN_PROBABILITY = 0.85` and lower `MIN_SEVERITY` to `0.0` in the next calibration rollout. Sol's estimated recall rises from 1.1% to 83.6%. The lower floor recovered 72 additional Sol-labeled defects alongside six additional labeled nondefects. Drop the zero-additional-false-positive rule. The lower floor grows the historical selection from 2 to 105, so validate that volume in shadow mode before enabling automatic posting.

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
