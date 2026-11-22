---
trigger: always_on
description: Always use interactive questions with recommendations and concise pros/cons when asking the user for decisions or clarifications.
---

# Interactive Questions With Recommendations & Trade-offs

When asking the user to make a decision, clarify requirements, or choose next
steps:

1. **Always ask interactively** using `ask_question` rather than leaving choices
   only in plain prose.
2. **State a clear recommendation** first (`(Recommended)` on the leading option).
3. **Include concise Pros and Cons** for every option in the accompanying
   response immediately above the `ask_question` prompt so trade-offs are clear
   before choosing.
