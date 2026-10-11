"use client";

import { useEffect, useRef, useState } from "react";
import { apiJson } from "@/lib/talos-client/api-json";
import type { RunTrace, TracePage } from "@/lib/contracts/observability";

export function TraceInspector() {
  const detailRef = useRef<HTMLElement>(null);
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState("");
  const [revision, setRevision] = useState(0);
  const [data, setData] = useState<TracePage | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [trace, setTrace] = useState<RunTrace | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState("");
  const [detailError, setDetailError] = useState("");

  useEffect(() => { if (trace) detailRef.current?.focus(); }, [trace]);

  useEffect(() => {
    const controller = new AbortController();
    const query = new URLSearchParams({ page: String(page) });
    if (status) query.set("status", status);
    void apiJson<TracePage>(`traces?${query}`, { signal: controller.signal }).then((value) => {
      if (!Array.isArray(value.traces) || !value.pagination) throw new Error("Trace list is unavailable.");
      if (!controller.signal.aborted) setData(value);
    }).catch((cause) => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "Could not load traces."); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [page, status, revision]);

  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    void apiJson<RunTrace>(`traces/${encodeURIComponent(selected)}`, { signal: controller.signal }).then((value) => {
      if (!Array.isArray(value.spans) || value.run_id !== selected) throw new Error("Run details are unavailable.");
      if (!controller.signal.aborted) setTrace(value);
    }).catch((cause) => { if (!controller.signal.aborted) setDetailError(cause instanceof Error ? cause.message : "Could not load this run."); })
      .finally(() => { if (!controller.signal.aborted) setDetailLoading(false); });
    return () => controller.abort();
  }, [selected, revision]);

  function refresh() { setLoading(true); setError(""); setDetailError(""); if (selected) { setTrace(null); setDetailLoading(true); } setRevision((value) => value + 1); }
  function changePage(next: number) { setLoading(true); setError(""); setData(null); setPage(next); }
  function selectRun(id: string) { if (id === selected) return; setTrace(null); setDetailError(""); setDetailLoading(true); setSelected(id); }

  return <section className="trace-inspector" aria-label="Agent run traces">
    <div className="trace-controls"><label>Run status<select value={status} onChange={(event) => { setStatus(event.target.value); changePage(1); setSelected(null); setTrace(null); setDetailLoading(false); setDetailError(""); }}>{["", "completed", "failed", "cancelled", "running"].map((value) => <option key={value} value={value}>{value || "All runs"}</option>)}</select></label><button disabled={loading} onClick={() => refresh()} type="button">Refresh traces</button></div>
    {error && <p role="alert">{error} <button type="button" onClick={() => refresh()}>Retry traces</button></p>}
    {loading && <p role="status">Loading traces…</p>}
    {!loading && !error && data?.traces.length === 0 && <p>No recorded runs match this filter. Send a message, then refresh.</p>}
    {!error && <ul className="trace-run-list" aria-label="Recorded runs">{data?.traces.map((run) => <li key={run.run_id}><button type="button" aria-pressed={selected === run.run_id} onClick={() => selectRun(run.run_id)}><strong>{run.model}</strong><span>{run.state} · {run.duration_ms.toLocaleString()} ms · {run.tool_calls} tool calls</span><time dateTime={run.started_at}>{new Date(run.started_at).toLocaleString()}</time><small>Run {run.run_id}</small></button></li>)}</ul>}
    {data && !error && <nav aria-label="Trace pages" className="trace-controls"><button type="button" disabled={loading || !data.pagination.has_previous} onClick={() => changePage(page - 1)}>Previous runs</button><span>Page {data.pagination.page} of {Math.max(1, data.pagination.total_pages)}</span><button type="button" disabled={loading || !data.pagination.has_next} onClick={() => changePage(page + 1)}>Next runs</button></nav>}
    {detailLoading && <p role="status">Loading run details…</p>}
    {detailError && <p role="alert">{detailError} <button type="button" onClick={() => refresh()}>Retry run details</button></p>}
    {trace && <article ref={detailRef} tabIndex={-1} className="trace-detail" aria-label="Run details"><h3>Run details</h3><p>{trace.provider} · {trace.model} · {trace.state}</p><dl className="trace-facts"><div><dt>Duration</dt><dd>{trace.duration_ms.toLocaleString()} ms</dd></div><div><dt>Input / output tokens</dt><dd>{trace.usage.input_tokens.toLocaleString()} / {trace.usage.output_tokens.toLocaleString()}</dd></div><div><dt>Tool calls / retries</dt><dd>{trace.tool_calls} / {trace.retries}</dd></div><div><dt>Configuration</dt><dd>v{trace.configuration_version}</dd></div></dl>
      {trace.error && <p role="alert">{trace.error.code}: {trace.error.message}</p>}
      <h4>Execution stages</h4>{trace.spans.length === 0 && <p>No stage timings were retained for this run.</p>}<ol className="trace-stage-list">{trace.spans.map((span) => <li key={span.span_id}><strong>{span.name}</strong><span>{span.status} · {span.duration_ms.toLocaleString()} ms</span>{typeof span.attributes["tool.name"] === "string" && <small>Tool: {span.attributes["tool.name"]}{typeof span.attributes["tool.outcome"] === "string" ? ` · ${span.attributes["tool.outcome"]}` : ""}</small>}</li>)}</ol>
      <details><summary>Run identifiers</summary><dl><dt>Run</dt><dd>{trace.run_id}</dd><dt>Conversation</dt><dd>{trace.conversation_id}</dd><dt>Configuration hash</dt><dd>{trace.configuration_hash}</dd></dl></details>
    </article>}
  </section>;
}
