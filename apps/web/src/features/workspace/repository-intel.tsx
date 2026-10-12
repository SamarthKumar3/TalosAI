"use client";

import { useEffect, useRef, useState } from "react";
import { CloseIcon, SparklesIcon } from "@/components/icons";
import { apiJson } from "@/lib/talos-client/api-json";

export type SelectedRepository = { id: string; name: string };
type IndexJob = { job_id: string; repository_id: string; file_count: number; skipped: number };

export function repositoryLink(input: string): string | null {
  const match = /https:\/\/github\.com\/([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)(?:\/)?(?=\s|$)/i.exec(input.trim());
  return match ? match[0].trim().replace(/\/$/, "") : null;
}

export function useRepositoryIntel(
  conversationId: string | null, selected: SelectedRepository | null | undefined,
  onSelected: (id: string, repository: SelectedRepository | null) => void,
) {
  const [selection, setSelection] = useState<{ id: string | null; enabled: boolean } | null>(null);
  const [credentialDraft, setCredentialDraft] = useState<{ id: string | null; value: string } | null>(null);
  const token = credentialDraft?.id === conversationId ? credentialDraft.value : "";
  const setToken = (value: string) => setCredentialDraft({ id: conversationId, value });
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const job = useRef<string | null>(null);
  const active = selection?.id === conversationId ? selection.enabled : Boolean(selected);
  useEffect(() => () => controller.current?.abort(), []);

  async function cancel() {
    controller.current?.abort();
    if (job.current) {
      try { await apiJson(`repositories/jobs/${job.current}/cancel`, { method: "POST" }); }
      catch { setError("Could not cancel the indexing job. Check its status before retrying."); }
    }
  }

  async function remove() {
    if (busy) return;
    setToken(""); setError(null);
    setSelection({ id: conversationId, enabled: false });
    if (conversationId && selected) {
      try {
        await apiJson(`conversations/${conversationId}/repository`, { method: "POST", body: JSON.stringify({ repository_id: null }) });
        onSelected(conversationId, null);
      } catch {
        setSelection({ id: conversationId, enabled: true });
        setError("Could not remove the repository selection. Try again.");
      }
    }
  }

  async function prepare(input: string, id: string): Promise<string> {
    if (!active) return input;
    setSelection({ id, enabled: true });
    const url = repositoryLink(input);
    if (!url) return input; // The runtime asks for a URL when no repository has been selected.
    const request = new AbortController();
    controller.current = request;
    setBusy(true); setError(null); setStatus("Reading safe source files from GitHub…");
    job.current = null;
    const credential = token.trim() || null;
    setToken("");
    try {
      const imported = await apiJson<IndexJob>("repositories/url", {
        method: "POST", signal: request.signal,
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ url, token: credential }),
      }, 100_000);
      job.current = imported.job_id;
      setStatus(`Indexing ${imported.file_count} files${imported.skipped ? ` (${imported.skipped} skipped)` : ""}…`);
      const deadline = Date.now() + 180_000;
      while (true) {
        request.signal.throwIfAborted();
        const result = await apiJson<{ state: string }>(`repositories/jobs/${imported.job_id}`, { signal: request.signal });
        if (result.state === "completed") break;
        if (["failed", "cancelled"].includes(result.state)) throw new Error(`Repository indexing ${result.state}. Check access and retry.`);
        if (Date.now() >= deadline) throw new Error(`Indexing is still running (job ${imported.job_id}). Retry when it has completed.`);
        await new Promise((resolve) => window.setTimeout(resolve, 750));
      }
      const result = await apiJson<{ repository: SelectedRepository }>(`conversations/${id}/repository`, {
        method: "POST", signal: request.signal, body: JSON.stringify({ repository_id: imported.repository_id }),
      });
      onSelected(id, result.repository);
      return input.replace(url, "").trim() || "Explain this repository's architecture and entry points with source citations.";
    } catch (reason) {
      if (request.signal.aborted) throw new Error("Repository import stopped. Your draft is restored.");
      throw reason;
    } finally {
      setBusy(false); setStatus(null); job.current = null; controller.current = null;
    }
  }

  return { active, busy, token, setToken, status, error, selected, prepare, cancel, remove,
    activate: () => { setError(null); setSelection({ id: conversationId, enabled: true }); },
  };
}

export type RepositoryIntelAction = ReturnType<typeof useRepositoryIntel>;

export function RepositoryIntelChip({ action }: Readonly<{ action: RepositoryIntelAction }>) {
  return <div className="repository-intel-controls">
    <div className="repository-intel-chip">
      <SparklesIcon /><strong>Repository Intel</strong>
      {action.selected && <span className="repository-selected" title={action.selected.name}>{action.selected.name}</span>}
      <button aria-label="Remove Repository Intel" disabled={action.busy} onClick={() => void action.remove()} type="button"><CloseIcon /></button>
    </div>
    <details className="repository-auth">
      <summary>Private repository?</summary>
      <label>GitHub token <input aria-label="GitHub repository token" type="password" autoComplete="off" maxLength={255} value={action.token} onChange={(event) => action.setToken(event.target.value)} disabled={action.busy} /></label>
      <p>Use a fine-grained token with Contents read-only access to this repository. Used for this import only. Never paste a token into chat.</p>
    </details>
    <p className="repository-privacy">Source excerpts are sent to your configured model. Only GitHub repository URLs are supported.</p>
    {action.status && <div className="repository-import-status"><span role="status">{action.status}</span><button onClick={() => void action.cancel()} type="button">Cancel import</button></div>}
    {action.error && <p role="alert">{action.error}</p>}
  </div>;
}
