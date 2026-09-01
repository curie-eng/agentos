## Summary

<!-- One paragraph: what changed, and why. Skip the play by play. -->

## Related issue

<!-- Closes #NNN. Use `Ref #NNN` if this does not fully close the issue. -->

Closes #

## Fix pin verification

<!-- Verification is required for declared fixes. For a fix pull request,
     replace this comment with exactly one selector changed by this pull request:
Fix pin: <supported selector>

     Supported selector forms:
     apps/*/tests/*.py::test
     packages/*/tests/*.py::test
     runner/tests/*.py::test
     cli/tests/name.rs::test
     charts/curie/ci/name.sh

     The declaration must be present before opening the pull request. If it is
     added or corrected later, the body edit automatically revalidates the
     required gate.

     REQUIRED when this pull request closes an issue labeled `bug` (a GitHub
     closing keyword plus a same-repo #N): CI fails without a Fix pin line. If
     there is no selector to declare (a revert, a docs-only fix, or a bug
     closed by deletion), use the escape hatch instead, with a non-empty
     reason:
Fix pin: n/a - <reason>

     For a non fix pull request that does not close a bug-labeled issue, leave
     this section empty. -->

## End-to-end verification

<!-- Choose exactly one path.

     Behavior-bearing: keep the tier table and three evidence checkboxes below.
     Classify every tier required or n/a with a concrete reason, and paste the
     exact command plus what you observed for each required tier.

     No runtime behavior: delete the tier table and three evidence checkboxes.
     Replace them with one explanation and the scoped checks you ran:

     This change does not alter runtime behavior because <reason>.
     Scoped verification: `<command>` - <observed outcome>.

     Documentation and ADR changes are not automatically exempt. If they alter
     runtime behavior, use the behavior-bearing path. See "E2E verification is
     mandatory" in AGENTS.md. -->

| Tier | Required / n/a | Reason | Mode (fake / live) | Command and observed outcome |
| --- | --- | --- | --- | --- |
| skill | | | | |
| local | | | | |
| local-release | | | | |
| cluster | | | | |
| live provider | | | | |
| external integration | | | | |

- [ ] Every required tier above names its exact command, the commit it ran
      against, the mode it ran in, and the literal outcome observed.
- [ ] Each meaningful acceptance criterion has positive proof plus a falsifiable
      negative or a second independent path.
- [ ] No required tier is left unproved; any blocked tier names its blocker in
      the table.

## Checklist

- [ ] Tests pass for the area I touched (see CONTRIBUTING.md for the commands).
- [ ] Docs updated if behavior changed.
- [ ] An ADR is added under `docs/adr/` if this is an architectural decision.
