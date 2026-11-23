---
trigger: always_on
description: Always use interactive questions with recommendations and concise pros/cons when asking the user for decisions or clarifications.
---

# Interactive Questions With Recommendations & Trade-offs

When asking the user to make a decision, clarify requirements, or choose next
steps:

1. **Research first before asking**: inspect available skills, OpenWiki, docs,
   and upstream or offline code first so every option and trade-off is grounded
   in verified facts rather than guesswork.
2. **Always ask interactively** using `ask_question` (including when surfacing
   blockers or planning choices) rather than leaving options only in plain prose.
3. **State a clear recommendation** first (`(Recommended)` on the leading option).
4. **Include concise Pros and Cons** for every option in the accompanying
   response immediately above the `ask_question` prompt so trade-offs are clear
   before choosing.
