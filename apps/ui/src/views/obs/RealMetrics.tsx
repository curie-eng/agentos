import { useState } from "react";
import { C } from "../../tokens";
import { Card, Chip, CliHint, AreaChart, Notice, cliCommand } from "../../primitives";
import { useStore } from "../../state/store";
import { useMetricsSummary, useMetricSeries } from "../../api/hooks";
import { formatLatency } from "../../lib/format";
import type { MetricsSummary, MetricKey, Granularity, MetricFilter } from "../../api/client";

// The five metrics the OB1 API exposes (Langfuse-backed; no Prometheus).
const METRICS: { key: MetricKey; label: string; unit: string }[] = [
  { key: "runs", label: "Runs", unit: "" },
  { key: "latency_p95_ms", label: "Latency p95", unit: "ms" },
  { key: "tokens", label: "Tokens", unit: "" },
  { key: "cost_usd", label: "Cost", unit: "$" },
  { key: "error_rate", label: "Error rate", unit: "%" },
];
const GRANULARITIES: Granularity[] = ["hour", "day", "week"];

function fmt(key: MetricKey, v: number): string {
  switch (key) {
    case "runs":
      return String(Math.round(v));
    case "latency_p95_ms":
      return formatLatency(v);
    case "tokens":
      return v >= 1000 ? (v / 1000).toFixed(1) + "k" : String(Math.round(v));
    case "cost_usd":
      return "$" + v.toFixed(2);
    case "error_rate":
      return (v <= 1 ? v * 100 : v).toFixed(1) + "%";
  }
}

function summaryValue(s: MetricsSummary, key: MetricKey): number {
  return s[key];
}

// Wired Metrics tab: the summary stat row + a selectable time-series chart, both
// from the OB1 API. Deliberately diverges from the canon's Prometheus PromQL bar
// and the p50/tool-calls/active-sessions panels (the API is Langfuse aggregates
// over five metrics), while keeping the card-grid + hero-chart layout.
export function RealMetrics() {
  const { state } = useStore();
  const [metric, setMetric] = useState<MetricKey>("runs");
  const [granularity, setGranularity] = useState<Granularity>("day");
  const [agentInput, setAgentInput] = useState("");
  const [agent, setAgent] = useState("");

  const filter: MetricFilter = {
    environment: state.env,
    agent: agent || undefined,
  };
  const summary = useMetricsSummary(true, filter);
  const series = useMetricSeries(true, metric, granularity, filter);
  const selected = METRICS.find((m) => m.key === metric)!;

  return (
    <div>
      {/* query/filter bar */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16, flexWrap: "wrap" }}>
        <div
          style={{
            flex: 1,
            minWidth: 260,
            display: "flex",
            alignItems: "center",
            gap: 8,
            background: C.input,
            border: "1px solid " + C.border,
            borderRadius: 8,
            padding: "8px 11px",
            fontFamily: C.mono,
            fontSize: 12.5,
          }}
        >
          <span style={{ color: C.brand }}>langfuse</span>
          <span style={{ color: C.text2 }}>
            {`{ metric="${metric}", env="${state.env}"${agent ? `, agent~"${agent}"` : ""} }`}
          </span>
          <span style={{ marginLeft: "auto", color: C.muted }}>last 7 days</span>
        </div>
        <input
          data-testid="metric-agent-filter"
          value={agentInput}
          placeholder="filter by agent (name contains)"
          onChange={(e) => setAgentInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") setAgent(agentInput.trim());
          }}
          onBlur={() => setAgent(agentInput.trim())}
          style={{
            width: 220,
            background: C.input,
            border: "1px solid " + C.borderStrong,
            borderRadius: 8,
            padding: "8px 11px",
            color: C.text,
            fontFamily: C.mono,
            fontSize: 12.5,
          }}
        />
        <CliHint
          command={cliCommand(
            state.env === "prod"
              ? "cluster.observability.metrics"
              : "local.observability.metrics",
            {
              metric,
              granularity,
              environment: state.env,
              agent: agent || undefined,
            },
          )}
        />
      </div>

      {/* summary stat cards */}
      {summary.error ? (
        <Card style={{ marginBottom: 16 }}>
          <Notice>Could not load metrics: {summary.error}</Notice>
        </Card>
      ) : (
        <div data-testid="metric-summary" style={{ display: "grid", gridTemplateColumns: "repeat(5,1fr)", gap: 14, marginBottom: 16 }}>
          {METRICS.map((m) => (
            <Card key={m.key} style={{ padding: "14px 16px" }}>
              <div style={{ fontSize: 12, color: C.muted, marginBottom: 6 }}>{m.label}</div>
              <div style={{ fontSize: 22, fontWeight: 400, fontFamily: C.mono }}>
                {summary.loading || !summary.data ? "…" : fmt(m.key, summaryValue(summary.data, m.key))}
              </div>
            </Card>
          ))}
        </div>
      )}

      {/* series chart with metric + granularity selectors */}
      <Card>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12, flexWrap: "wrap" }}>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {METRICS.map((m) => {
              const on = m.key === metric;
              return (
                <button
                  key={m.key}
                  type="button"
                  onClick={() => setMetric(m.key)}
                  style={{
                    fontFamily: C.mono,
                    fontSize: 12,
                    padding: "4px 10px",
                    borderRadius: 20,
                    cursor: "pointer",
                    background: on ? "rgba(62,207,142,.08)" : "transparent",
                    color: on ? C.brand : C.muted,
                    border: "1px solid " + (on ? "rgba(62,207,142,.4)" : C.border),
                  }}
                >
                  {m.label}
                </button>
              );
            })}
          </div>
          <div style={{ marginLeft: "auto", display: "flex", background: C.card, border: "1px solid " + C.border, borderRadius: 8, padding: 2 }}>
            {GRANULARITIES.map((g) => (
              <button
                key={g}
                type="button"
                onClick={() => setGranularity(g)}
                style={{
                  fontFamily: C.mono,
                  fontSize: 12,
                  padding: "4px 10px",
                  borderRadius: 6,
                  border: "none",
                  cursor: "pointer",
                  background: granularity === g ? C.hover : "transparent",
                  color: granularity === g ? C.text : C.muted,
                }}
              >
                {g}
              </button>
            ))}
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "baseline", marginBottom: 6 }}>
          <div style={{ fontSize: 13, color: C.text2 }}>{selected.label} over time</div>
          <Chip color={C.mutedStatus} border={C.border}>
            {series.data ? series.data.granularity : granularity}
          </Chip>
        </div>
        {series.error ? (
          <Notice>Could not load series: {series.error}</Notice>
        ) : series.loading || !series.data ? (
          <Notice>Loading…</Notice>
        ) : series.data.points.length === 0 ? (
          <Notice>No data in this window.</Notice>
        ) : (
          <div data-testid="metric-chart">
            <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 6 }}>
              <span style={{ fontFamily: C.mono, fontSize: 20, color: C.text }} data-testid="metric-chart-latest">
                {fmt(metric, series.data.points[series.data.points.length - 1].value)}
              </span>
              <span style={{ fontSize: 12, color: C.muted }}>
                latest of {series.data.points.length} {series.data.points.length === 1 ? "point" : "points"}
              </span>
            </div>
            <AreaChart data={series.data.points.map((p) => p.value)} color={C.brand} height={120} />
          </div>
        )}
      </Card>
    </div>
  );
}
