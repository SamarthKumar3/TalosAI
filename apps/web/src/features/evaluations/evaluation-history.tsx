"use client";

import type { EChartsOption } from "echarts";

import { AnalyticsChart } from "@/components/analytics-chart";
import type { EvaluationRun } from "@/lib/contracts";

function palette() {
  return ["var(--accent)", "var(--color-cyan)", "var(--color-warning)", "var(--danger)"];
}

function pie(data: Array<{ name: string; value: number }>): EChartsOption {
  return { color: palette(), tooltip: { trigger: "item", formatter: "{b}: {c} ({d}%)" }, legend: { bottom: 0 }, series: [{ type: "pie", radius: ["42%", "70%"], center: ["50%", "44%"], data }] };
}

export function EvaluationHistory({ runs }: { runs: EvaluationRun[] }) {
  const completed = runs.filter((run) => run.state === "completed" && run.summary);
  const quality = completed.length
    ? completed.reduce((total, run) => total + (run.summary?.candidate.pass_rate ?? 0), 0) / completed.length
    : 0;
  const statuses = ["completed", "failed", "cancelled"].map((name) => ({ name, value: runs.filter((run) => run.state === name).length }));
  const frameworks = Object.entries(runs.reduce<Record<string, number>>((totals, run) => {
    totals[run.evaluator_version] = (totals[run.evaluator_version] ?? 0) + 1; return totals;
  }, {})).map(([name, value]) => ({ name, value }));
  const byDay = Object.entries(runs.reduce<Record<string, number>>((totals, run) => {
    const day = run.created_at.slice(0, 10); totals[day] = (totals[day] ?? 0) + 1; return totals;
  }, {})).sort(([left], [right]) => left.localeCompare(right));
  const gauge: EChartsOption = { color: palette(), series: [{ type: "gauge", min: 0, max: 100, progress: { show: true }, detail: { formatter: "{value}%" }, data: [{ value: Math.round(quality * 100), name: "Candidate pass rate" }] }] };
  const line: EChartsOption = { color: palette(), tooltip: { trigger: "axis" }, xAxis: { type: "category", data: byDay.map(([day]) => day) }, yAxis: { type: "value", minInterval: 1 }, series: [{ name: "Evaluations", type: "line", smooth: true, data: byDay.map(([, value]) => value) }] };

  if (runs.length === 0) return <p className="monitoring-empty">Run the first evaluation to build history.</p>;
  return <>
    <div className="analytics-grid evaluation-analytics">
      <section className="monitoring-card"><h3>Average eval quality score</h3><AnalyticsChart label="Average evaluation quality score" option={gauge} /></section>
      <section className="monitoring-card"><h3>Evaluation sessions</h3><AnalyticsChart label="Completed, failed, and cancelled evaluation sessions" option={pie(statuses)} /></section>
      <section className="monitoring-card"><h3>Framework distribution</h3><AnalyticsChart label="Evaluator framework distribution" option={pie(frameworks)} /></section>
      <section className="monitoring-card"><h3>Evaluations per day</h3><AnalyticsChart label="Number of evaluations by day" option={line} /></section>
    </div>
    <section className="evaluation-history" aria-labelledby="evaluation-history-title"><div className="monitoring-card-heading"><div><span>Same dataset, paired outcomes</span><h3 id="evaluation-history-title">Evaluation runs</h3></div><strong>{runs.length} runs</strong></div>
      <div className="evaluation-run-list">{runs.map((run) => <details className="evaluation-run" key={run.id}><summary><span><strong>{run.state}</strong><small>{new Date(run.created_at).toLocaleString()}</small></span><span className={run.summary?.gate.passed ? "gate-pass" : "gate-neutral"}>{run.summary ? `${Math.round(run.summary.candidate.pass_rate * 100)}% quality` : "No score"}</span></summary><dl className="trace-facts"><div><dt>Framework</dt><dd>{run.evaluator_version}</dd></div><div><dt>Results</dt><dd>{run.result_count}</dd></div><div><dt>Attempts</dt><dd>{run.attempt_count}</dd></div><div><dt>Gate</dt><dd>{run.summary?.gate.passed ? "Passed" : "Not passed"}</dd></div></dl></details>)}</div>
    </section>
  </>;
}
