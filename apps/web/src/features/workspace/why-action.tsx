"use client";

import { type RefObject, useEffect, useRef, useState } from "react";
import { apiJson } from "../../lib/talos-client/api-json";
import { CloseIcon, PlusIcon } from "../../components/icons";

export type WhyTarget = {
  message_id: string; run_id: string | null; offset: number;
  response_number: number; created_at: string;
};
export type WhyHistory = {
  conversation_id: string; eligible_responses: number; start: number;
  has_more: boolean; targets: WhyTarget[];
};
export type WhyReport = {
  submitted_at?: string;
  conversation_id: string; target: WhyTarget;
  sections: { title: string; status: string; facts: string[]; records: Record<string, unknown>[] }[];
  limitations: string[];
};

const EMPTY_REPORTS: WhyReport[] = [];

type Selection = { conversationId: string; target: WhyTarget };

export function useWhyAction(conversationId: string | null, eligible: boolean, cancelSpeech: () => void, inputLockedRef: RefObject<boolean>) {
  const [selection, setSelection] = useState<Selection | null>(null);
  const [history, setHistory] = useState<WhyHistory | null>(null);
  const [reports, setReports] = useState<Record<string, WhyReport[]>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const lock = useRef(false);
  const generation = useRef(0);
  const active = selection?.conversationId === conversationId && selection !== null;

  useEffect(() => {
    generation.current += 1;
    inputLockedRef.current = false;
  }, [conversationId, inputLockedRef]);

  async function activate() {
    if (!conversationId || !eligible || lock.current) return;
    cancelSpeech();
    inputLockedRef.current = true;
    lock.current = true;
    setBusy(true);
    setError(null);
    const current = generation.current;
    try {
      const page = await apiJson<WhyHistory>(`conversations/${conversationId}/why/history`);
      if (current !== generation.current) return;
      if (!page.targets[0]) throw new Error("There are no previous Talos responses to inspect.");
      setHistory(page);
      setSelection({ conversationId, target: page.targets[0] });
    } catch (cause) {
      if (current === generation.current) setError(cause instanceof Error ? cause.message : "Could not load response history.");
      inputLockedRef.current = false;
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }

  async function page(start: number) {
    if (!conversationId || lock.current) return;
    lock.current = true;
    setBusy(true);
    setError(null);
    const current = generation.current;
    try {
      const result = await apiJson<WhyHistory>(`conversations/${conversationId}/why/history?start=${start}&limit=20`);
      if (current === generation.current) setHistory(result);
    } catch (cause) {
      if (current === generation.current) setError(cause instanceof Error ? cause.message : "Could not load older responses.");
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }

  function remove() {
    if (lock.current) return;
    setSelection(null);
    inputLockedRef.current = false;
    setError(null);
  }

  async function send() {
    if (!active || !selection || lock.current) return;
    lock.current = true;
    setBusy(true);
    setError(null);
    const current = generation.current;
    const { target } = selection;
    try {
      const report = await apiJson<WhyReport>(`conversations/${conversationId}/why?offset=${target.offset}&expected_message_id=${encodeURIComponent(target.message_id)}`);
      if (current !== generation.current) return;
      report.submitted_at = new Date().toISOString();
      setReports((all) => ({ ...all, [report.conversation_id]: [...(all[report.conversation_id] ?? []), report] }));
      setSelection(null);
      inputLockedRef.current = false;
    } catch (cause) {
      if (current === generation.current) setError(cause instanceof Error ? cause.message : "Could not inspect this response.");
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }

  return {
    active, busy, error, eligible, activate, remove, send, page, inputLockedRef,
    target: active ? selection.target : null,
    history: history?.conversation_id === conversationId ? history : null,
    reports: conversationId ? reports[conversationId] ?? EMPTY_REPORTS : EMPTY_REPORTS,
    select(target: WhyTarget) {
      if (conversationId && !lock.current) setSelection({ conversationId, target });
    },
  };
}

export type WhyAction = ReturnType<typeof useWhyAction>;

export function WhyMenu({ action, disabled, onRepositoryIntel }: Readonly<{ action: WhyAction; disabled: boolean; onRepositoryIntel?: () => void }>) {
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  const option = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    option.current?.focus();
    function close(event: PointerEvent) {
      if (!menu.current?.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [open]);
  return <div className="why-menu" ref={menu} onKeyDown={(event) => {
    if (event.key === "Escape") { setOpen(false); button.current?.focus(); }
  }}>
    <button aria-label="Composer actions" aria-expanded={open} aria-controls="composer-actions" className="attachment-button" disabled={disabled || action.active || action.busy} onClick={() => setOpen(!open)} ref={button} type="button"><PlusIcon /></button>
    {open && <div className="why-popover" id="composer-actions" role="group" aria-label="Actions">
      <strong>Actions</strong>
      <button disabled={!action.eligible} ref={option} type="button" onClick={() => { setOpen(false); void action.activate(); }}>
        <strong>Why</strong><span>Explain and debug a previous Talos reply</span>
      </button>
      {!action.eligible && <p>There are no previous Talos responses to inspect.</p>}
      {onRepositoryIntel && <button type="button" onClick={() => { setOpen(false); onRepositoryIntel(); }}><strong>Repository Intel</strong><span>Ask about a GitHub repository or create its HLD</span></button>}
    </div>}
  </div>;
}

export function WhyChip({ action }: Readonly<{ action: WhyAction }>) {
  const history = action.history;
  const target = action.target;
  if (!target) return null;
  const targets = history?.targets ?? [];
  return <div className="why-chip" title="Why - Ask Talos to explain and debug the previous message.">
    <strong>Why</strong>
    <label className="sr-only" htmlFor="why-target">Inspect Talos response</label>
    <select aria-describedby="why-description" disabled={action.busy} id="why-target" value={target.message_id} onChange={(event) => {
      const selected = targets.find((item) => item.message_id === event.target.value);
      if (selected) action.select(selected);
    }}>
      {!targets.some((item) => item.message_id === target.message_id) && <option value={target.message_id}>Response #{target.response_number} ({target.offset})</option>}
      {targets.map((item) => <option key={item.message_id} value={item.message_id}>{item.offset === -1 ? "Latest reply" : `Response #${item.response_number}`} ({item.offset}) - {new Date(item.created_at).toLocaleString()}</option>)}
    </select>
    {history && history.start > 0 && <button aria-label="Newer responses" disabled={action.busy} onClick={() => void action.page(Math.max(0, history.start - 20))} type="button">Newer</button>}
    {history?.has_more && <button aria-label="Older responses" disabled={action.busy} onClick={() => void action.page(history.start + 20)} type="button">Older</button>}
    <button aria-label="Remove Why action" disabled={action.busy} onClick={action.remove} type="button"><CloseIcon /></button>
    <span className="sr-only" id="why-description">Why - Ask Talos to explain and debug the previous message. Your draft and attachments are preserved. Normal input is disabled.</span>
  </div>;
}

export function WhyReportCard({ report }: Readonly<{ report: WhyReport }>) {
  return <article className="why-report" aria-label="Why decision autopsy">
    <h3>Why - Decision Autopsy</h3>
    <p>Inspecting Talos response #{report.target.response_number} ({report.target.offset})</p>
    <a href={`#message-${report.target.message_id}`}>View original response</a>
    <p>Run: {report.target.run_id ?? "Unavailable"}</p>
    {report.sections.map((section) => <details key={section.title} open={section.title === "Observed execution explanation" || section.title === "Run metadata"}>
      <summary>{section.title} - {section.status}</summary>
      {section.facts.map((fact, i) => <p key={i}>{fact}</p>)}
      {section.records.map((record, i) => <dl key={i}>{Object.entries(record).map(([key, value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{typeof value === "object" ? <pre>{JSON.stringify(value, null, 2)}</pre> : String(value)}</dd></div>)}</dl>)}
    </details>)}
    <h4>Limitations</h4><ul>{report.limitations.map((text) => <li key={text}>{text}</li>)}</ul>
  </article>;
}
