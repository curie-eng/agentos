# Verification contract (fixture)

A miniature stand-in for the published verification contract. Every command
named below resolves in the fixture command manifest that sits beside this
tree, and the command gate is what keeps that true.

House rule, stated here because the gate depends on it: a command is only
checked when it sits inside backticks. A command written in bare prose is not
scanned.

## Outcome to command

- After bringing the tier up: `curie skill status --json` exits 0 and reports
  a running session.
- After a deploy: `curie skill eval --json` exits 0 and reports zero failures.
- To grade a different case file, `curie skill eval --cases` names it.
- Offline plumbing proof, no credential required: `curie skill check --json`
  exits 0.
