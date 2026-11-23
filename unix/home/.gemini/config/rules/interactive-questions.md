---
trigger: always_on
description: Always use interactive questions with recommendations and concise pros/cons when asking the user for decisions or clarifications.
---

# Interactive Questions With Recommendations & Trade-offs

When asking the user to make a decision, clarify requirements, or choose next
steps:

1. **Research first before asking**: explore the codebase, available skills,
   OpenWiki, docs, and upstream or offline source to resolve everything code
   can settle upfront, so every option and trade-off is grounded in verified
   facts instead of guesswork.
2. **Always ask interactively** using `ask_question` (including when surfacing
   blockers or planning choices) rather than leaving options only in plain prose.
3. **State a clear recommendation** first (`(Recommended)` on the leading option).
4. **Include concise Pros and Cons** for every option in the accompanying
   response immediately above the `ask_question` prompt so trade-offs are clear
   before choosing.
5. **Orchestrator plan/execute hand-back**: when running on an execution
   turn (Plan Mode off) and user decisions or next-step choices are needed,
   hand back via `enter_plan_mode` (or call `ask_question` in a dedicated
   short turn) rather than ending a long summary with prose options.
6. **Ask-question `/learn` boundary**: `ask_question` does not trigger the
   `Stop` hook; after finishing a mutating task before asking the user a
   follow-up question, run one bounded `/learn` check first.
7. **Branch coupled decisions (`/grill-me` style)**: when later choices depend
   on earlier answers, resolve them iteratively (one focused branch at a time)
   instead of dumping a speculative matrix; suggest `/grill-me` or
   `/plan /grill-me` when a broad design needs a full upfront interview.
