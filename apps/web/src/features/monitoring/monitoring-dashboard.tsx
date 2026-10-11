"use client";

import type { EChartsOption } from "echarts";
import { useEffect, useMemo, useState } from "react";

import { AnalyticsChart } from "@/components/analytics-chart";
import type { ObservabilityAlert, TracePage, UsageReport } from "@/lib/contracts";
import { apiJson } from "@/lib/talos-client/api-json";

type WindowChoice = "10" | "30" | "60" | "custom";
type GroupChoice = "time" | "model" | "provider";
type MetricChoice = "tokens" | "cost";

const emptyPage: TracePage = {
  traces: [],
  pagination: { page: 1, page_size: 15, total_items: 0, total_pages: 0, has_previous: false, has_next: false },
};

function colors() {
  return ["var(--accent)", "var(--color-cyan)", "var(--color-warning)", "var(--danger)"];
}

function baseOption(): EChartsOption {
  return {
    color: colors(),
    textStyle: { color: "var(--ink-muted)" },
    tooltip: { trigger: "axis" },
    legend: { bottom: 0, textStyle: { color: "var(--ink-muted)" } },
    grid: { left: 54, right: 20, top: 24, bottom: 50 },
    xAxis: { type: "category", axisLabel: { color: "var(--ink-muted)" }, axisLine: { lineStyle: { color: "var(--line)" } } },
    yAxis: { type: "value", axisLabel: { color: "var(--ink-muted)" }, splitLine: { lineStyle: { color: "var(--line)" } } },
  };
}

function lineOption(labels: string[], series: Array<{ name: string; data: number[] }>): EChartsOption {
  const base = baseOption();
  return {
    ...base,
    xAxis: { ...(base.xAxis as object), data: labels },
    series: series.map((item, index) => ({
      ...item, type: "line", smooth: false, showSymbol: true, symbolSize: 5,
      symbol: ["circle", "rect", "triangle", "diamond"][index % 4],
      lineStyle: { type: index % 2 ? "dashed" : "solid" },
    })),
  };
}

function pieOption(data: Array<{ name: string; value: number }>): EChartsOption {
  return {
    color: colors(),
    tooltip: { trigger: "item" },
    legend: { bottom: 0, textStyle: { color: "var(--ink-muted)" } },
    series: [{ type: "pie", radius: ["42%", "70%"], center: ["50%", "44%"], data, label: { color: "var(--ink)" } }],
  };
}

function money(value: number | null, currency: string | null) {
  if (value === null) return "Unknown";
  return new Intl.NumberFormat("en-US", { style: "currency", currency: currency || "USD", maximumFractionDigits: 6 }).format(value);
}

export function MonitoringDashboard({ refreshKey }: { refreshKey: string | null }) {
  const [usage, setUsage] = useState<UsageReport | null>(null);
  const [page, setPage] = useState<TracePage>(emptyPage);
  const [alerts, setAlerts] = useState<ObservabilityAlert[]>([]);
  const [pageNumber, setPageNumber] = useState(1);
  const [windowChoice, setWindowChoice] = useState<WindowChoice>("10");
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd] = useState("");
  const [groupBy, setGroupBy] = useState<GroupChoice>("time");
  const [metric, setMetric] = useState<MetricChoice>("tokens");
  const [model, setModel] = useState("");
  const [provider, setProvider] = useState("");
  const [status, setStatus] = useState("");
  const [costSort, setCostSort] = useState<"asc" | "desc" | "">("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const usageQuery = new URLSearchParams({ group_by: groupBy, metric });
    if (windowChoice === "custom") {
      if (customStart) usageQuery.set("start_date", customStart);
      if (customEnd) usageQuery.set("end_date", customEnd);
    } else usageQuery.set("days", windowChoice);
    const traceQuery = new URLSearchParams({ page: String(pageNumber) });
    if (model) traceQuery.set("model", model);
    if (provider) traceQuery.set("provider", provider);
    if (status) traceQuery.set("status", status);
    if (costSort) traceQuery.set("cost_sort", costSort);
    void Promise.all([
      apiJson<UsageReport>(`usage?${usageQuery}`),
      apiJson<TracePage>(`traces?${traceQuery}`),
      apiJson<{ alerts: ObservabilityAlert[] }>("alerts"),
    ]).then(([usageValue, pageValue, alertValue]) => {
      if (!active) return;
      if (!usageValue.summary || !usageValue.daily || !pageValue.pagination) {
        throw new Error("Monitoring API is out of date. Rebuild the Talos API service.");
      }
      setUsage(usageValue); setPage(pageValue); setAlerts(alertValue.alerts); setError(null);
    }).catch((caught: unknown) => {
      if (active) setError(caught instanceof Error ? caught.message : "Could not load monitoring data.");
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [costSort, customEnd, customStart, groupBy, metric, model, pageNumber, provider, refreshKey, status, windowChoice]);

  const summaryCards = useMemo(() => usage ? [
    ["Input tokens", usage.input_tokens.toLocaleString()], ["Output tokens", usage.output_tokens.toLocaleString()],
    ["Sessions", usage.summary.sessions.toLocaleString()], ["Total spend", money(usage.estimated_cost, usage.currency)],
    ["Total tokens", usage.summary.total_tokens.toLocaleString()], ["LLM calls", usage.model_calls.toLocaleString()],
    ["Messages", usage.summary.messages.toLocaleString()], ["Average latency", `${usage.summary.average_latency_ms.toLocaleString()} ms`],
    ["Avg cost / call", money(usage.summary.average_cost_per_call, usage.currency)], ["Daily avg cost", money(usage.summary.daily_average_cost, usage.currency)],
    ["Daily avg tokens", usage.summary.daily_average_tokens.toLocaleString()], ["Avg tokens / call", usage.summary.average_tokens_per_call.toLocaleString()],
    ["Peak usage date", usage.summary.peak_usage_date ?? "No usage"], ["Peak usage tokens", usage.summary.peak_usage_tokens.toLocaleString()],
  ] : [], [usage]);

  const labels = usage?.daily.map((point) => point.date.slice(5)) ?? [];
  const tokenTrend = usage ? lineOption(labels, [
    { name: "Input tokens", data: usage.daily.map((point) => point.input_tokens) },
    { name: "Output tokens", data: usage.daily.map((point) => point.output_tokens) },
  ]) : {};
  const usageTrend = usage ? lineOption(labels, usage.usage_series.map((series) => ({ name: series.name, data: series.points.map((point) => point.value) }))) : {};
  const latencyTrend = usage ? lineOption(labels, [
    { name: "Average", data: usage.daily.map((point) => point.average_latency_ms) },
    { name: "P90", data: usage.daily.map((point) => point.p90_latency_ms) },
    { name: "P95", data: usage.daily.map((point) => point.p95_latency_ms) },
    { name: "P99", data: usage.daily.map((point) => point.p99_latency_ms) },
  ]) : {};
  const messageTrend = usage ? lineOption(labels, [{ name: "Messages", data: usage.daily.map((point) => point.messages) }]) : {};

  function resetPage(setter: (value: string) => void, value: string) { setter(value); setPageNumber(1); }

  return (
    <>
      {loading && <p className="monitoring-empty" role="status">Loading monitoring data…</p>}
      {error && <p className="settings-status" role="alert">{error}</p>}
      {alerts.length > 0 && <section className="observability-alerts" aria-label="Active alerts">{alerts.map((alert) => <article className={`observability-alert ${alert.severity}`} key={alert.code}><strong>{alert.code.replaceAll("_", " ")}</strong><p>{alert.message}</p></article>)}</section>}

      <fieldset className="analytics-filters"><legend>Usage view</legend>
        <label>Date range<select value={windowChoice} onChange={(event) => setWindowChoice(event.target.value as WindowChoice)}><option value="10">10 days</option><option value="30">30 days</option><option value="60">60 days</option><option value="custom">Custom</option></select></label>
        {windowChoice === "custom" && <><label>Start<input type="date" value={customStart} onChange={(event) => setCustomStart(event.target.value)} /></label><label>End<input type="date" value={customEnd} onChange={(event) => setCustomEnd(event.target.value)} /></label></>}
        <label>Group by<select value={groupBy} onChange={(event) => setGroupBy(event.target.value as GroupChoice)}><option value="time">Time</option><option value="model">Model</option><option value="provider">Provider</option></select></label>
        <label>Metric<select value={metric} onChange={(event) => setMetric(event.target.value as MetricChoice)}><option value="tokens">Tokens</option><option value="cost">Cost</option></select></label>
      </fieldset>

      <div className="metric-grid analytics-metrics">{summaryCards.map(([label, value]) => <article className="metric-card" key={label}><span>{label}</span><strong>{value}</strong></article>)}</div>
      {usage && <div className="analytics-grid">
        <section className="monitoring-card"><h3>Input and output token trend</h3><AnalyticsChart label="Input and output token trend" option={tokenTrend} /></section>
        <section className="monitoring-card"><h3>Completed and failed chats</h3><AnalyticsChart label="Completed and failed chat distribution" option={pieOption(usage.statuses.filter((item) => item.name === "completed" || item.name === "failed"))} /></section>
        <section className="monitoring-card"><h3>Models by total tokens</h3><AnalyticsChart label="Model token distribution" option={pieOption(usage.models)} /></section>
        <section className="monitoring-card analytics-wide"><h3>Total usage per day</h3><AnalyticsChart label={`Daily ${metric} grouped by ${groupBy}`} option={usageTrend} /></section>
        <section className="monitoring-card analytics-wide"><h3>Latency percentiles</h3><AnalyticsChart label="Daily average, P90, P95, and P99 latency" option={latencyTrend} /></section>
        <section className="monitoring-card analytics-wide"><h3>Messages trend</h3><AnalyticsChart label="Messages per day" option={messageTrend} /></section>
      </div>}

      <section className="monitoring-card usage-summary" aria-labelledby="usage-summary-title">
        <div className="monitoring-card-heading"><div><span>One row per LLM query trace</span><h3 id="usage-summary-title">User usage summary</h3></div><strong>{page.pagination.total_items} calls</strong></div>
        <div className="table-filters">
          <label>Model<select value={model} onChange={(event) => resetPage(setModel, event.target.value)}><option value="">All models</option>{usage?.filter_options.models.map((item) => <option key={item}>{item}</option>)}</select></label>
          <label>Provider<select value={provider} onChange={(event) => resetPage(setProvider, event.target.value)}><option value="">All providers</option>{usage?.filter_options.providers.map((item) => <option key={item}>{item}</option>)}</select></label>
          <label>Status<select value={status} onChange={(event) => resetPage(setStatus, event.target.value)}><option value="">All statuses</option>{usage?.filter_options.statuses.map((item) => <option key={item}>{item}</option>)}</select></label>
        </div>
        <div className="usage-table-wrap"><table><thead><tr><th>Model</th><th>Provider</th><th>Input / output tokens</th><th><button aria-label="Sort by cost" onClick={() => { setCostSort((value) => value === "asc" ? "desc" : "asc"); setPageNumber(1); }} type="button">Cost {costSort === "asc" ? "↑" : costSort === "desc" ? "↓" : "↕"}</button></th><th>Status</th><th>Timestamp</th></tr></thead>
          <tbody>{page.traces.map((trace) => <tr key={trace.run_id}><td>{trace.model}</td><td>{trace.provider}</td><td>{trace.usage.input_tokens.toLocaleString()} / {trace.usage.output_tokens.toLocaleString()}</td><td>{money(trace.usage.estimated_cost, trace.usage.currency)}</td><td><span className={`trace-status trace-status-${trace.state}`}>{trace.state}</span></td><td><time dateTime={trace.started_at}>{new Date(trace.started_at).toLocaleString()}</time></td></tr>)}</tbody></table></div>
        {!loading && page.traces.length === 0 && <p className="monitoring-empty">No traces match these filters.</p>}
        <nav aria-label="Usage summary pages" className="table-pagination"><button disabled={!page.pagination.has_previous} onClick={() => setPageNumber((value) => value - 1)} type="button">Previous</button><span>Page {page.pagination.page} of {Math.max(1, page.pagination.total_pages)}</span><button disabled={!page.pagination.has_next} onClick={() => setPageNumber((value) => value + 1)} type="button">Next</button></nav>
      </section>
    </>
  );
}
