---
name: git-flow-deploy
description: Passing evidence for Curie's signed Git flow deployment and promotion path.
---

# Git flow deploy

## Passing evidence

A passing Git flow deploy run accepts a correctly signed push, resolves the target agent, selects the permitted deployment environment for the pushed ref, clones the trusted source, archives the produced artifact, and processes the push into the resolved agent's deployment. `apps/api/src/curie_api/routers/github.py:github_webhook` and `apps/api/src/curie_api/gitflow.py:verify_signature`, `environment_for_ref`, `clone_and_archive`, `process_push`, and `resolve_target_agent` define these steps. ADR-0091 defines the deployment intent.

A passing run must demonstrate the supported dev deployment and main promotion paths, artifact reuse for the promotion, and isolation between different agents. It must also demonstrate rejection of an invalid signature, an ignored branch, an untrusted origin, and a foreign agent. `apps/api/tests/test_gitflow_integration.py` contains the named dev deploy, main promotion, signature rejection, ignored branch, trusted origin rejection, different agents, exact artifact reuse, and foreign agent rejection tests.

## What a passing run does not prove

A passing Git flow deploy run does not prove that every branch is deployable, that every GitHub webhook is trusted, or that every repository may deploy to every agent. The accepted environments and agent ownership are resolved from the incoming ref and target agent rather than inferred as universal permissions. `apps/api/src/curie_api/gitflow.py:environment_for_ref` and `resolve_target_agent`, plus the rejection tests in `apps/api/tests/test_gitflow_integration.py`, bound that claim.

A passing run does not prove that a deployed artifact is correct for every future runtime request. It proves the signed push was processed through the Git flow path and that the tested deployment boundaries held for that run. ADR-0091 and `apps/api/tests/test_gitflow_integration.py` set this boundary.
