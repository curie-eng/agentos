## Summary

<!-- One paragraph: what changed, and why. Skip the play by play. -->

## Related issue

<!-- Closes #NNN. Use `Ref #NNN` if this does not fully close the issue. -->

Closes #

## Release train

<!-- Use main for general bugs, security fixes, and shared changes. Use next only
     for v0.7 features or bugs unique to unreleased v0.7 work. -->

- [ ] This PR targets `main`.
- [ ] This PR targets `next`.

## End-to-end verification

<!-- Behavior-bearing changes only. Classify every tier required or n/a with a
     concrete reason, and paste the exact command plus what you observed for
     each required tier. See "E2E verification is mandatory" in AGENTS.md. -->

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
- [ ] Or: this change is not behavior-bearing, so no tier applies, and the
      summary says why.

## Checklist

- [ ] Tests pass for the area I touched (see CONTRIBUTING.md for the commands).
- [ ] Docs updated if behavior changed.
- [ ] An ADR is added under `docs/adr/` if this is an architectural decision.
