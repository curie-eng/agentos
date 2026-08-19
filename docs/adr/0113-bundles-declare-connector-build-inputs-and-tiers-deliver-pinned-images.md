# 113. Bundles declare connector build inputs and tiers deliver pinned images

Date: 2026-08-19
Status: Draft

Issue: #1690

## Context

[ADR 0086](0086-bundles-declare-connectors-the-platform-hosts-them.md) made
connectors part of a bundle declaration and assigned hosting to Curie. [ADR
0087](0087-the-api-renders-connector-objects-the-cli-applies-them.md) retained
the API as a pure renderer and assigned cluster application to the operator's
CLI.

That model accepts an `image:` in `connectors.yaml`, but not a source directory.
`examples/sre-bot` ships `connectors/k8s-write` and `connectors/tempo` as
Docker build contexts. Before either connector can deploy, an operator must
build it, put it somewhere the target can pull it, and replace a source
description with an image reference. The resulting manual state is not a
reviewable part of the bundle.

The platform has previously failed when a mutable image tag changed underneath
a deployment. Connector builds must therefore yield an immutable image identity
that the rendered connector object can use without another tag resolution.

The parity ladder also gives each tier different artifact transport:

| Tier | Connector result required |
| --- | --- |
| skill | No hosted connector. The declaration remains reported but cannot be exercised. |
| local | An image in the local Docker daemon used by the local connector container. |
| cluster | An image available to cluster nodes by a registry pull using an immutable digest. |

`tempo` also demonstrates that the builder architecture and the target node
architecture can differ. The build path needs an observable incompatibility
failure rather than a late container crash.

## Decision

**A bundle declares connector source build input. Curie builds that declared
input before deploy, resolves it to a digest, and renders or starts only the
digest pinned image.**

The declaration is a `build:` form in `connectors.yaml`, mutually exclusive
with the existing `image:` and `url:` forms. The exact schema is a separate
compatible change to the frozen `plugin-format` interface. It must constrain a
build context to the extracted bundle and identify the Dockerfile and target
platform without accepting arbitrary host paths.

The declaration, rather than a command invocation, is the source of truth. An
operator command may execute the declared build as part of the existing build
and deploy workflow, but it must not take an unrecorded connector directory or
leave the resulting image identity only in local CLI state.

For the local tier, the build result is loaded into the local Docker daemon and
the local connector container uses its digest pinned reference. For the cluster
tier, the portable delivery path pushes the built artifact to a configured
registry, obtains its manifest digest, and passes that digest to the existing
API rendering and CLI application flow. Node image import may support a
disposable local cluster test, but is not a deployable artifact transport
contract because it depends on individual node state.

The renderer and applier never deploy a mutable connector tag. A build either
produces a pullable digest for the selected tier and target architecture, or
fails before connector objects are applied. The resolved digest is the only
identity rendered into a Deployment or used to start the local connector.

This does not change the skill tier. It continues to report a hosted connector
as declared but not exercisable, as ADR 0086 requires.

## Consequences

The bundle remains reviewable: a source connector, its build settings, and the
requested target platform all appear in the versioned bundle. The manual
build, push, and image line edit sequence disappears from the normal local and
cluster deployment path.

Connector source builds introduce a distinct build boundary. They must not run
inside the internet facing API, which remains a pure renderer under ADR 0087.
They run under the operator controlled CLI path, which already owns local Docker
access, registry credentials, and cluster application credentials. Build secrets
and registry credentials remain local to that path and never enter the rendered
connector objects.

The implementation must add parity evidence for all applicable tiers. A source
only connector must run locally from the local daemon and deploy to a cluster
from its pushed digest. A changed source must produce a distinct digest. A
missing registry artifact or incompatible architecture must fail before a
connector Deployment is applied. The skill tier must continue to report the
connector as not exercisable rather than attempting a build or host operation.

No implementation is authorized by this Draft. A maintainer must accept this
ADR, then the frozen connector schema change and its callers must land as their
own reviewed compatible change before build execution or delivery work begins.

## Alternatives considered

### A standalone connector build CLI verb

Rejected. A command such as `curie build extension` can be a useful executor,
but it cannot be the declaration. Its source directory and output identity
would be absent from the bundle review and a rebuild from the same bundle would
not know what to build. This conflicts with ADR 0086's declarative connector
model.

### Build during cluster deployment

Rejected. It would couple source build tools, registry access, and potentially
slow or architecture dependent work to cluster application. It also obscures
whether a failure came from build, artifact delivery, or Kubernetes apply. The
operator build step can fail before the API renders and the CLI applies.

### Keep manual build, push, and image edits

Rejected. It leaves a connector's source and deployed image disconnected and
makes the ordinary source shipped connector path dependent on unreproducible
operator memory.

### Deploy a mutable image tag

Rejected. A tag can change after review and between tier runs, reproducing the
mutable tag failure mode. A manifest digest identifies the exact artifact that
the local or cluster tier will run.

### Make node image import the cluster delivery contract

Rejected. Import can be useful in a disposable local cluster, but nodes can be
replaced or scaled independently. A registry referenced by digest gives every
node the same retrievable artifact and works outside one developer machine.
