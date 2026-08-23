import { useCallback, useEffect, useState } from "react";
import { C } from "../../tokens";
import { Button, Card, Chip, Dot, Modal, Notice, Table } from "../../primitives";
import { useStore } from "../../state/store";
import {
  ApiError,
  getApprovalAudit,
  listApprovals,
  resolveApproval,
  type ApprovalAudit,
  type ApprovalOut,
} from "../../api/client";

// The operator visibility + resolve surface for durable approvals (#867,
// ADR-0010). The worker creates an approval when a run pauses on a
// permission/policy gate and suspends the session; today the only place to see
// or act on one is the Slack card. This tab lists them (pending by default) and
// drives the resolve-once route from the console, so an operator has visibility
// and control outside Slack. Read + resolve only; it never creates approvals
// (that is the worker's job). Backed by the real API over the same-origin /api
// proxy (GET /approvals, GET /approvals/{id}/audit, POST /approvals/{id}/resolve).

// The status filter options; "all" sends no status_filter so every status
// returns. Pending is the default — the queue an operator acts on.
const STATUS_FILTERS: [string, string][] = [
  ["pending", "Pending"],
  ["approved", "Approved"],
  ["rejected", "Rejected"],
  ["expired", "Expired"],
  ["all", "All"],
];

// Persist the resolving operator's identity so they need not retype it each
// time; resolved_by is required server-side and gates self-approval.
const OPERATOR_KEY = "curie.approvalOperator";

function loadOperator(): string {
  try {
    return window.localStorage.getItem(OPERATOR_KEY) ?? "";
  } catch {
    return "";
  }
}

function saveOperator(value: string): void {
  try {
    window.localStorage.setItem(OPERATOR_KEY, value);
  } catch {
    // Non-fatal: a private-mode / disabled localStorage just means no memory.
  }
}

function statusColor(status: string): string {
  switch (status) {
    case "pending":
      return C.warn;
    case "approved":
      return C.success;
    case "rejected":
      return C.failure;
    case "expired":
      return C.mutedStatus;
    default:
      return C.mutedStatus;
  }
}

function StatusChip({ status }: { status: string }) {
  const color = statusColor(status);
  return (
    <Chip color={color} border={C.border} pre={<Dot color={color} size={7} />}>
      {status}
    </Chip>
  );
}

function formatWhen(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

// Compact relative age, e.g. "3m ago", "2h ago", "5d ago"; "just now" under a
// minute, and a friendly future form for an expiry that has not passed yet.
function relativeAge(iso: string | null): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const deltaMs = Date.now() - then;
  const future = deltaMs < 0;
  const secs = Math.abs(deltaMs) / 1000;
  const label =
    secs < 60
      ? "just now"
      : secs < 3600
        ? `${Math.floor(secs / 60)}m`
        : secs < 86400
          ? `${Math.floor(secs / 3600)}h`
          : `${Math.floor(secs / 86400)}d`;
  if (label === "just now") return label;
  return future ? `in ${label}` : `${label} ago`;
}

function ApprovalFacts({ approval }: { approval: ApprovalOut }) {
  return (
    <dl
      style={{
        display: "grid",
        gridTemplateColumns: "auto 1fr",
        gap: "6px 14px",
        margin: 0,
        fontSize: 12.5,
        marginBottom: 16,
      }}
    >
      {(
        [
          ["Author", approval.author],
          ["Route", approval.route ?? "— (requesting channel)"],
          ["Card channel", approval.card_channel ?? "—"],
          ["Conversation", approval.conversation_id],
          ["Created", formatWhen(approval.created_at)],
          ["Expires", approval.expires_at ? `${formatWhen(approval.expires_at)} (${relativeAge(approval.expires_at)})` : "—"],
          ["Resolved by", approval.resolved_by ?? "—"],
          ["Resolution note", approval.resolution_note ?? "—"],
        ] as [string, string][]
      ).map(([k, v]) => (
        <div key={k} style={{ display: "contents" }}>
          <dt style={{ color: C.muted }}>{k}</dt>
          <dd style={{ margin: 0, color: C.text2, fontFamily: C.mono, wordBreak: "break-word" }}>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

function AuditTrail({ audit, auditError }: { audit: ApprovalAudit[] | null; auditError: string | null }) {
  return (
    <div style={{ borderTop: "1px solid " + C.border, paddingTop: 14 }}>
      <div style={{ fontWeight: 600, fontSize: 13, color: C.text2, marginBottom: 8 }}>Audit trail</div>
      {auditError ? (
        <div data-testid="audit-error" style={{ color: C.destructive, fontSize: 12.5, fontFamily: C.mono }}>
          {auditError}
        </div>
      ) : audit === null ? (
        <Notice padding="16px">Loading audit…</Notice>
      ) : audit.length === 0 ? (
        <Notice padding="16px">No resolution attempts yet.</Notice>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {audit.map((entry) => (
            <div
              key={entry.id}
              data-testid="approval-audit-entry"
              style={{ border: "1px solid " + C.border, borderRadius: 6, padding: "8px 10px", background: C.darkest }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12.5 }}>
                <span style={{ fontFamily: C.mono, color: C.text }}>{entry.action}</span>
                <Chip color={entry.authorized ? C.success : C.failure} border={C.border}>
                  {entry.authorized ? "authorized" : "denied"}
                </Chip>
                <span style={{ marginLeft: "auto", color: C.muted, fontSize: 11, fontFamily: C.mono }}>
                  {formatWhen(entry.created_at)}
                </span>
              </div>
              <div style={{ color: C.text2, fontSize: 12, marginTop: 4 }}>
                {entry.actor} · {entry.decision} · via {entry.authorizer}
                {entry.reason ? ` — ${entry.reason}` : ""}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ApprovalDetail({
  approval,
  onClose,
  onResolved,
}: {
  approval: ApprovalOut;
  onClose: () => void;
  onResolved: () => void;
}) {
  const { dispatch } = useStore();
  const [audit, setAudit] = useState<ApprovalAudit[] | null>(null);
  const [auditError, setAuditError] = useState<string | null>(null);
  const [resolvedBy, setResolvedBy] = useState(loadOperator);
  const [actorChannel, setActorChannel] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<"approved" | "rejected" | null>(null);
  const [resolveError, setResolveError] = useState<string | null>(null);

  const pending = approval.status === "pending";

  useEffect(() => {
    let live = true;
    getApprovalAudit(approval.id)
      .then((rows) => {
        if (live) setAudit(rows);
      })
      .catch((e) => {
        if (live) setAuditError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      live = false;
    };
  }, [approval.id]);

  const resolve = async (decision: "approved" | "rejected") => {
    const who = resolvedBy.trim();
    if (!who) {
      setResolveError("Enter who is resolving this approval.");
      return;
    }
    setBusy(decision);
    setResolveError(null);
    try {
      await resolveApproval(approval.id, {
        decision,
        resolved_by: who,
        note: note.trim() || undefined,
        actor_channel: actorChannel.trim() || undefined,
      });
      saveOperator(who);
      dispatch({ type: "toast", message: `Approval ${decision}` });
      onResolved();
    } catch (e) {
      // The resolve route has designed failure statuses; surface each honestly
      // rather than a bare "failed" (403 self-approval/not-authorized, 409 lost
      // the race, 410 expired past its SLA).
      const msg =
        e instanceof ApiError
          ? e.status === 403
            ? `Not authorized: ${e.message}`
            : e.status === 409
              ? `Already resolved: ${e.message}`
              : e.status === 410
                ? `Expired: ${e.message}`
                : `${e.status}: ${e.message}`
          : e instanceof Error
            ? e.message
            : String(e);
      setResolveError(msg);
    } finally {
      setBusy(null);
    }
  };

  const inputStyle = {
    background: C.input,
    border: "1px solid " + C.borderStrong,
    borderRadius: 7,
    padding: "8px 11px",
    color: C.text,
    fontSize: 13,
    width: "100%",
    boxSizing: "border-box" as const,
  };

  return (
    <Modal onClose={onClose}>
      <div
        data-testid="approval-detail"
        style={{
          width: "min(680px, 90vw)",
          maxHeight: "85vh",
          overflowY: "auto",
          background: C.card,
          border: "1px solid " + C.borderStrong,
          borderRadius: 14,
          padding: 22,
        }}
      >
        <div style={{ display: "flex", alignItems: "flex-start", gap: 12, marginBottom: 14 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 15, fontWeight: 600, color: C.text, marginBottom: 6 }}>{approval.summary}</div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
              <StatusChip status={approval.status} />
              {approval.gate_kind ? (
                <Chip color={C.text2} border={C.border}>{`gate: ${approval.gate_kind}`}</Chip>
              ) : null}
              {approval.granted_tool ? (
                <Chip color={C.text2} border={C.border}>{approval.granted_tool}</Chip>
              ) : null}
            </div>
          </div>
          <Button label="Close" variant="ghost" size="sm" onClick={onClose} />
        </div>

        <ApprovalFacts approval={approval} />

        {pending ? (
          <div
            style={{
              borderTop: "1px solid " + C.border,
              paddingTop: 14,
              marginBottom: 16,
              display: "flex",
              flexDirection: "column",
              gap: 10,
            }}
          >
            <div style={{ fontWeight: 600, fontSize: 13, color: C.text2 }}>Resolve</div>
            <input
              aria-label="resolved by"
              value={resolvedBy}
              onChange={(e) => setResolvedBy(e.target.value)}
              placeholder="Your identity (e.g. you@example.com)"
              style={inputStyle}
            />
            <input
              aria-label="actor channel"
              value={actorChannel}
              onChange={(e) => setActorChannel(e.target.value)}
              placeholder="Actor channel (optional, e.g. C0123ABCD)"
              style={inputStyle}
            />
            <textarea
              aria-label="note"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Note (optional)"
              rows={2}
              style={{ ...inputStyle, resize: "vertical", fontFamily: C.sans }}
            />
            {resolveError ? (
              <div data-testid="resolve-error" style={{ color: C.destructive, fontSize: 12.5, fontFamily: C.mono }}>
                {resolveError}
              </div>
            ) : null}
            <div style={{ display: "flex", gap: 10 }}>
              <Button
                label={busy === "approved" ? "Approving…" : "Approve"}
                variant="primary"
                testId="approve-btn"
                disabled={busy !== null}
                onClick={() => void resolve("approved")}
              />
              <Button
                label={busy === "rejected" ? "Rejecting…" : "Reject"}
                variant="danger"
                testId="reject-btn"
                disabled={busy !== null}
                onClick={() => void resolve("rejected")}
              />
            </div>
          </div>
        ) : null}

        <AuditTrail audit={audit} auditError={auditError} />
      </div>
    </Modal>
  );
}

export function RealApprovals() {
  const [status, setStatus] = useState("pending");
  const [approvals, setApprovals] = useState<ApprovalOut[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const rows = await listApprovals({ status: status === "all" ? undefined : status });
      setApprovals(rows);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [status]);

  useEffect(() => {
    void load();
  }, [load]);

  const open = approvals?.find((a) => a.id === openId) ?? null;

  const inputStyle = {
    background: C.input,
    border: "1px solid " + C.borderStrong,
    borderRadius: 7,
    padding: "7px 10px",
    color: C.text,
    fontSize: 12.5,
  };

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
        <select
          data-testid="approvals-status-filter"
          aria-label="status filter"
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          style={inputStyle}
        >
          {STATUS_FILTERS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
        <div style={{ color: C.muted, fontSize: 12.5, flex: 1 }}>
          Human approval gates that paused a run. Resolve here or from the Slack card.
        </div>
        <Button label="Refresh" variant="ghost" size="sm" onClick={() => void load()} />
      </div>

      {error ? (
        <div data-testid="approvals-error" style={{ color: C.destructive, fontSize: 12.5, marginBottom: 10, fontFamily: C.mono }}>
          {error}
        </div>
      ) : null}

      <Card>
        <div data-testid="approvals">
          {loading ? (
            <Notice padding="28px 20px">Loading approvals…</Notice>
          ) : !approvals || approvals.length === 0 ? (
            <Notice padding="28px 20px">
              {status === "pending"
                ? "No pending approvals. Nothing is waiting on a human decision."
                : `No ${status === "all" ? "" : status + " "}approvals to show.`}
            </Notice>
          ) : (
            <Table
              columns="minmax(0,2fr) minmax(0,1fr) minmax(0,1fr) auto auto"
              headers={["Summary", "Author", "Route", "Status", "Age"]}
              rows={approvals.map((a) => ({
                key: a.id,
                onClick: () => setOpenId(a.id),
                accent: a.status === "pending" ? C.warn : undefined,
                cells: [
                  <span data-testid="approval-summary" style={{ color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", display: "block" }}>
                    {a.summary}
                  </span>,
                  <span style={{ color: C.text2, fontFamily: C.mono, fontSize: 12 }}>{a.author}</span>,
                  <span style={{ color: C.muted, fontFamily: C.mono, fontSize: 12 }}>{a.route ?? "—"}</span>,
                  <StatusChip status={a.status} />,
                  <span style={{ color: C.muted, fontFamily: C.mono, fontSize: 12 }}>{relativeAge(a.created_at)}</span>,
                ],
              }))}
            />
          )}
        </div>
      </Card>

      {open ? (
        <ApprovalDetail
          approval={open}
          onClose={() => setOpenId(null)}
          onResolved={() => {
            setOpenId(null);
            void load();
          }}
        />
      ) : null}
    </div>
  );
}
