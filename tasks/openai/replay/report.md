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

Recommendation is `MIN_PROBABILITY = 0.85` and `MIN_SEVERITY = 1.0`. The conservative change rule requires at least 10 labeled defects, an F1 gain of at least 0.05, and zero additional labeled false positives.

## Recurring false positives

- incorrect — 4

For the review prompt, require direct patch evidence for each factual claim and omit candidates that depend on missing implementation.

## Limitations

The grader is a model, and shares its model with production triage. Agreement can reflect shared blind spots. Follow-up source and patches are byte-bounded excerpts. A later edit is corroboration only. A lack of a later edit is inconclusive. Path renames, squashes, and unavailable context can hide a real fix. The sample includes only the two chosen repositories and only findings returned on their admitted patches.
