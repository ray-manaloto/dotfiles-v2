# Review standards

Judgement-call rules for Stage 1 review (`/code-review:code-review`, independent review subagents and
fleet reviewers). Mechanical rules belong in `hk.pkl` or `mise doctor project`, not here. Apply every
section that matches the diff, and report each miss as a finding with `path:line` and a concrete
failure scenario.

## Credential and config migrations step by step

When a plan or diff migrates credentials or config (token rotation or revocation, secret moves, branch
or config inheritance changes):

- Write out the state after each step in execution order: which credential or config each live
  consumer (CI job, hook, service, guest, script) reads, and whether that credential is still valid at
  that point.
- Confirm every live consumer still authenticates at every step, not only at the end. Flag a step that
  revokes or rotates a credential before the change that stops using it has merged and deployed, two
  steps that rotate the same secret on different paths, and new branches or configs that inherit a
  value the plan assumed was replaced.
- In scripts or runbooks the owner will run, check every CLI subcommand and flag against that tool's
  `--help` (or primary docs) for the installed version; never assume a flag such as `--yes` exists.

## Degraded input fails closed

A check, gate or guard that cannot read its input (a missing or failing tool such as `lsof`, a stale
tracking ref, a malformed HTTP body, a timeout, an empty list where one entry was expected) reports
that it could not decide and names what it could not read. It never passes, and never reports a guess
as a result. (#47, #49, #52)

## Destructive automation selects by allowlist

Anything that kills, deletes, closes, prunes or overwrites picks its targets with an allowlist of known
states, and treats an unknown or new state as keep. Check remote or foreign objects (another machine's
paths, another session's terminals) and symlinked paths explicitly. (#130: a status denylist would have
closed blocked and remote-machine Herdr tabs.)

## Narrowed environments resolve the program first

When code narrows `PATH` or the environment for a subprocess (`env -i`, `env -u`, a minimal `PATH`),
it resolves the executable before narrowing, or proves the narrowed `PATH` still contains it. (#123)

## No secrets in error text, no bulk data in argv

- Error, log and status text never interpolates URLs, headers or environment values that can carry
  credentials (push URLs with tokens, `Authorization` headers). (#49)
- Unbounded lists (refs, file names, records) reach a subprocess through stdin or a file, never argv,
  where they can hit `E2BIG`. (#69)
