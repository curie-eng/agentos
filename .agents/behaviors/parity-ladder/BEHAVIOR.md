---
name: parity-ladder
description: Passing evidence for Curie's parity ladder across skill, local, local-release, and cluster.
---

# Parity ladder

## Passing evidence

A passing parity ladder run uses the same weather bundle and the same case identity across every selected rung. `cli/scripts/e2e-ladder.sh` and ADR-0081 define this parity contract.

The fake CI jobs `e2e-ladder`, `e2e-ladder-release`, and `e2e-ladder-cluster` prove selected rung plumbing, the weather bundle and case identity, and finalized nonempty replies where the rung produces a reply. They do not prove semantic correctness or use of a real model. `cli/scripts/e2e-ladder.sh` and `.github/workflows/ci.yaml` define this fake evidence.

The live nightly jobs `ladder-skill-local`, `ladder-local-release`, and `ladder-cluster` add a content graded skill result and a non fake sentinel negative control on live reply rungs. `.github/workflows/nightly-graded-ladder.yaml` and ADR-0081 define this live evidence.

## What a passing run does not prove

The `local`, `local-release`, and `cluster` reply evidence does not grade reply semantics. A finalized nonempty reply is runtime evidence, not a claim that the reply is useful, correct, or equivalent in content to the content graded `skill` reply. `cli/scripts/e2e-ladder.sh` and ADR-0081 distinguish reply evidence from content grading.

Issue #1622 reports that the `ladder-skill-local`, `ladder-local-release`, and `ladder-cluster` jobs in `.github/workflows/nightly-graded-ladder.yaml` recorded 10 passing runs out of 20 when the issue was filed. That is historical operating evidence only. It is not a passing threshold or a reliability guarantee for a future run.
