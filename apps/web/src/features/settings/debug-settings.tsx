"use client";

import { useState } from "react";
import { apiJson } from "@/lib/talos-client/api-json";

type DoctorReport = {
  healthy: boolean;
  checks: { name: string; status: "ok" | "error"; message: string }[];
};

export function DebugSettings() {
  const [report, setReport] = useState<DoctorReport | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");

  async function runDoctor() {
    setRunning(true);
    setError("");
    setReport(null);
    try {
      setReport(await apiJson<DoctorReport>("doctor", { method: "POST" }, 45_000));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Cannot reach Talos. Start Docker and reopen Talos with talos ui.");
    } finally {
      setRunning(false);
    }
  }

  return <section className="settings-view debug-settings" aria-busy={running}>
    <h3>Debug</h3>
    <p>Check your local settings, model key, Docker and Docker Compose. These checks do not change your data or call a model.</p>
    <button className="primary-button" disabled={running} onClick={() => void runDoctor()} type="button">{running ? "Checking…" : "Run Talos Doctor"}</button>
    <div aria-live="polite" role="status">{running ? "Running local diagnostics…" : report && (report.healthy ? "All checks passed." : "Some checks need attention.")}</div>
    {error && <p role="alert">{error} Run <code>talos doctor</code> in your terminal if the local service is unavailable.</p>}
    {report && <dl className="settings-list">{report.checks.map((check) => <div key={check.name}><dt>{check.name}</dt><dd><strong>{check.status === "ok" ? "Passed" : "Needs attention"}</strong><p>{check.message}</p></dd></div>)}</dl>}
  </section>;
}
