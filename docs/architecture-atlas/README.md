# Architecture atlas preview

This directory contains the versioned, interactive Curie architecture atlas.
The HTML is only a renderer: architecture facts, seams, flows, ADR links, and
version metadata live in JSON.

Once this directory is on `main`, open the
[GitHub-rendered atlas](https://htmlpreview.github.io/?https://github.com/curie-eng/curie/blob/main/docs/architecture-atlas/index.html)
without cloning the repository. During branch review, replace `main` in that URL
with the branch name.

From the repository root, serve it with:

```bash
python3 -m http.server 8767 --directory docs/architecture-atlas
```

Then open <http://localhost:8767/>. Opening `index.html` directly as a
`file://` URL does not work because the browser blocks or restricts the JSON
requests. Any ordinary static HTTP server works; no build or package install is
required.

Snapshots under `snapshots/` are immutable historical views. `versions.json`
selects the default and populates the version picker. Use the project-local
`update-architecture-atlas` skill to create and research a new snapshot.

Every tagged release must register a snapshot with the same version id before
the tag is pushed. Generate it from the final architecture-bearing commit, then
make only the release version edits. The release workflow fails before image or
binary builds if the snapshot is absent, its manifest and repository metadata
disagree, its pinned commit is not tag ancestry, or any path other than the
snapshot registration and release-coupled version files changed after the pin.
This ordering avoids the impossible requirement for a commit to contain its own
SHA while still proving the tag has no un-atlased architecture changes.
