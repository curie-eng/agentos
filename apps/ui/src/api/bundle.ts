// Packages the editor's skill.md into a Claude Code plugin bundle, client-side.
// jszip is the packaging choice: it is the de-facto browser zip library, ships
// its own types, and produces an archive that apps/api's zip intake accepts
// (a flat zip whose root holds .claude-plugin/plugin.json). The API validates
// the bytes via the frozen plugin_format validator, so this only has to lay out
// the canonical file tree:
//
//   .claude-plugin/plugin.json      (manifest: name required)
//   skills/<name>/SKILL.md          (the editor content; frontmatter name+description)

export interface BundleInput {
  agentName: string;
  versionLabel: string;
  skillMd: string;
}

// Pull `description:` out of the SKILL.md frontmatter for the manifest, so the
// bundle carries a real description rather than a placeholder.
function descriptionFrom(skillMd: string): string {
  const match = skillMd.match(/^\s*description:\s*(.+)$/m);
  return match ? match[1].trim() : "";
}

// The path -> content map for the bundle. Exposed as a seam so the layout can be
// unit-tested without a Blob round-trip (jsdom Blobs are not reliably readable).
export function bundleFileTree(input: BundleInput): Record<string, string> {
  const manifest = {
    name: input.agentName,
    version: input.versionLabel,
    description: descriptionFrom(input.skillMd) || `Agent ${input.agentName}`,
  };
  return {
    ".claude-plugin/plugin.json": JSON.stringify(manifest, null, 2),
    [`skills/${input.agentName}/SKILL.md`]: input.skillMd,
  };
}

export async function buildBundleZip(input: BundleInput): Promise<Blob> {
  return zipTree(bundleFileTree(input));
}

// ---- FX2 redeploy: re-pack an existing bundle's files with edits applied ----

const MANIFEST_PATH = ".claude-plugin/plugin.json";

export interface BundleFileEntry {
  path: string;
  content: string;
}

/**
 * Assemble the path->content tree for a redeploy from the version's existing
 * bundle files. Callers pass the fetched files (which may already include the
 * manifest and multiple skills) so nothing is silently dropped on redeploy; a
 * `.claude-plugin/plugin.json` is synthesized only if the bundle lacks one, so
 * the archive always satisfies the frozen plugin_format validator.
 */
export function bundleTreeFromFiles(agentName: string, files: BundleFileEntry[]): Record<string, string> {
  const tree: Record<string, string> = {};
  for (const f of files) tree[f.path] = f.content;
  if (!(MANIFEST_PATH in tree)) {
    const firstSkill = files.find((f) => f.path.endsWith("/SKILL.md") || f.path === "SKILL.md");
    const description = (firstSkill && descriptionFrom(firstSkill.content)) || `Agent ${agentName}`;
    tree[MANIFEST_PATH] = JSON.stringify({ name: agentName, description }, null, 2);
  }
  return tree;
}

async function zipTree(tree: Record<string, string>): Promise<Blob> {
  const { default: JSZip } = await import("jszip");
  const zip = new JSZip();
  for (const [path, content] of Object.entries(tree)) {
    zip.file(path, content);
  }
  return zip.generateAsync({ type: "blob", compression: "DEFLATE" });
}

export async function buildBundleZipFromFiles(agentName: string, files: BundleFileEntry[]): Promise<Blob> {
  return zipTree(bundleTreeFromFiles(agentName, files));
}

// Compute the next version label for a redeploy. Bumps the patch of the highest
// vX.Y.Z among existing labels; falls back to v0.1.<count> when none parse, and
// always returns a label not already present (suffixing -rN on any collision).
export function nextVersionLabel(existing: string[]): string {
  const semver = /^v(\d+)\.(\d+)\.(\d+)$/;
  let best: [number, number, number] | null = null;
  for (const raw of existing) {
    const m = raw.trim().match(semver);
    if (!m) continue;
    const t: [number, number, number] = [Number(m[1]), Number(m[2]), Number(m[3])];
    if (best === null || cmp(t, best) > 0) best = t;
  }
  const base = best ? `v${best[0]}.${best[1]}.${best[2] + 1}` : `v0.1.${existing.length}`;
  const taken = new Set(existing.map((s) => s.trim()));
  if (!taken.has(base)) return base;
  let n = 1;
  while (taken.has(`${base}-r${n}`)) n += 1;
  return `${base}-r${n}`;
}

function cmp(a: [number, number, number], b: [number, number, number]): number {
  for (let i = 0; i < 3; i += 1) {
    if (a[i] !== b[i]) return a[i] - b[i];
  }
  return 0;
}
