# 113. Bundles declare connector build inputs and tiers deliver pinned images

Date: 2026-08-19
Status: Accepted

Issue: #1690

## Context

[ADR 0086](0086-bundles-declare-connectors-the-platform-hosts-them.md) made
connectors part of a bundle declaration and assigned hosting to Curie. [ADR
0087](0087-the-api-renders-connector-objects-the-cli-applies-them.md) retained
the API as a pure renderer and assigned cluster application to the operator's
CLI.

MCP specifies the protocol between an MCP client and server. It does not specify
how a server is built, deployed, given credentials, or connected to the agent.
`.mcp.json` expresses an MCP connection, either to an HTTP endpoint or to a
stdio command. It does not say who runs that command or service.

`connectors.yaml` is Curie's declaration for a server it must host. It accepts
an `image:` today but not a source directory. `examples/sre-bot` ships
`connectors/k8s-write` and `connectors/tempo` as Docker build contexts. Before
either connector can deploy, an operator must build it, put it where the target
can pull it, and edit an image reference. The resulting manual state is not a
reviewable part of the bundle.

The platform has previously failed when a mutable image tag changed underneath
a deployment. Connector builds must therefore yield an immutable image identity
that the rendered connector object can use without another tag resolution.

The existing skill contract reports hosted connectors as declared but not
exercisable. That leaves a connector dependent skill untested until the local
or cluster rung and breaks the promised parity. `tempo` also demonstrates that
the builder architecture and the target node architecture can differ. The build
path needs an observable incompatibility failure rather than a late container
crash.

## Decision

**A bundle uses `.mcp.json` for external MCP servers and `connectors.yaml` for
MCP servers Curie hosts. Curie builds declared connector source, resolves the
result to a digest, and runs that digest at every hostable parity tier.**

An external MCP server belongs only in `.mcp.json`. A remote HTTP server remains
owned by its provider. A stdio server is valid when its executable is already in
the runner artifact. Curie does not download a package or execute an arbitrary
`uv` install while a skill starts.

A hosted connector belongs in `connectors.yaml`, whether its OCI image came
from a third party or from the bundle's own source. Curie generates the MCP
entry that points at a hosted connector. The bundle author does not repeat an
unstable service URL in `.mcp.json`.

The declaration is a `build:` form in `connectors.yaml`, mutually exclusive
with the existing `image:` and `url:` forms. The exact schema is a separate
compatible change to the frozen `plugin-format` interface. It must constrain a
build context to the extracted bundle and identify the Dockerfile and target
platform without accepting arbitrary host paths.

The declaration, rather than a command invocation, is the source of truth. An
operator command may execute the declared build as part of the existing build
and deploy workflow, but it must not take an unrecorded connector directory or
leave the resulting image identity only in local CLI state.

For skill, Curie starts the runner and each hosted connector from the local
Docker daemon on a private network. The runner receives only the generated
connector URLs and has no host port dependency. For local, Compose starts the
same digest pinned connector images and uses the same generated MCP entries.
For cluster, the portable delivery path pushes the built artifact to a
configured registry, obtains its manifest digest, and passes that digest to the
existing API rendering and CLI application flow. Node image import may support
a disposable local cluster test, but is not a deployable artifact transport
contract because it depends on individual node state.

The renderer and applier never deploy a mutable connector tag. A build either
produces a pullable digest for the selected tier and target architecture, or
fails before connector objects are applied. The resolved digest is the only
identity rendered into a Deployment or used to start the local connector.

The skill rung is no longer a connector free runner check when the bundle
declares hosted connectors. It is a Docker backed integration rung that proves
the runner can use the same hosted MCP server configuration as local and
cluster. A bundle with no hosted connectors keeps the existing hermetic skill
behavior.

## Consequences

The bundle remains reviewable: an external MCP connection, a hosted connector
source or image, its build settings, and the requested target platform all
appear in versioned files. The manual build, push, and image line edit sequence
disappears from the normal skill, local, and cluster path.

Connector source builds introduce a distinct build boundary. They must not run
inside the internet facing API, which remains a pure renderer under ADR 0087.
They run under the operator controlled CLI path, which already owns local Docker
access, registry credentials, and cluster application credentials. Build secrets
and registry credentials remain local to that path and never enter the rendered
connector objects.

The implementation must add parity evidence for all three hostable tiers using
the SRE bot. A source only connector must run beside the skill runner from the
local daemon, run in local Compose, and deploy to a cluster from its pushed
digest. A changed source must produce a distinct digest. A missing registry
artifact or incompatible architecture must fail before a connector Deployment
is applied. A remote third party entry in `.mcp.json` must remain external and
must not cause Curie to start a connector container.

The frozen connector schema change and its callers must land as their own
reviewed compatible change before build execution or delivery work begins.

## Alternatives considered

### Put every MCP server in `connectors.yaml`

Rejected. A remote third party HTTP endpoint and a stdio command that is already
available in the runner need no Curie hosted workload. Treating them as hosted
would duplicate the server's owner and blur the distinction between connecting
to an MCP server and operating one.

### Keep hosted connectors out of the skill rung

Rejected. It makes the first connector dependent test occur after the skill
rung and makes a skill dependent on a connector behave differently from local
and cluster. Curie already uses Docker for the runner, so a private Docker
network is the narrowest shared host substrate.

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
