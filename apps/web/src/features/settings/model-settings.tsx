"use client";

import { useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { apiJson } from "@/lib/talos-client/api-json";
import { CheckIcon, ChevronIcon } from "@/components/icons";

export type ModelCatalog = {
  providers: { id: string; label: string }[];
  credentials: { id: string; label: string; provider: string }[];
  models: { id: string; label: string; model_id: string; provider: string; credential_id: string }[];
  default_model_id: string;
};

export function validateModelCatalog(value: ModelCatalog): ModelCatalog {
  if (!Array.isArray(value.models) || !Array.isArray(value.credentials) || !Array.isArray(value.providers) || typeof value.default_model_id !== "string") throw new Error("Model configuration is unavailable. Refresh to retry.");
  return value;
}

export function ChatModelPicker({ catalog, disabled, onChange, value }: Readonly<{
  catalog: ModelCatalog | null;
  disabled: boolean;
  onChange: (id: string) => void;
  value: string;
}>) {
  const [open, setOpen] = useState(false);
  const menuId = useId();
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const selected = catalog?.models.find((model) => model.id === value);

  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector<HTMLButtonElement>('[aria-selected="true"]')?.focus();
  }, [open]);

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      setOpen(false);
      trigger.current?.focus();
    }
    if (open && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
      event.preventDefault();
      const options = [...(menu.current?.querySelectorAll<HTMLButtonElement>('[role="option"]') ?? [])];
      const current = options.indexOf(document.activeElement as HTMLButtonElement);
      const step = event.key === "ArrowDown" ? 1 : -1;
      options[(current + step + options.length) % options.length]?.focus();
    }
    if ((event.key === "ArrowDown" || event.key === "Enter" || event.key === " ") && !open) {
      event.preventDefault();
      setOpen(true);
    }
  }

  return (
    <div className="chat-model-select" onKeyDown={onKeyDown} ref={root}>
      <button
        aria-label={`Chat model: ${selected?.label ?? "Pinned model"}`}
        aria-controls={menuId}
        aria-expanded={open}
        aria-haspopup="listbox"
        className="chat-model-trigger"
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
        title="Chat model"
        type="button"
        ref={trigger}
      >
        <span>{selected?.label ?? "Pinned model"}</span>
        <ChevronIcon />
      </button>
      {open && !disabled && (
        <div aria-label="Chat model" className="chat-model-menu" id={menuId} ref={menu} role="listbox">
          {catalog?.models.map((model) => (
            <button
              aria-selected={model.id === value}
              key={model.id}
              onClick={() => {
                setOpen(false);
                if (model.id !== value) onChange(model.id);
              }}
              role="option"
              type="button"
            >
              <span><strong>{model.label}</strong><small>{model.provider}</small></span>
              {model.id === value && <CheckIcon />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

type Props = Readonly<{
  catalog: ModelCatalog | null;
  onChange: (catalog: ModelCatalog) => void;
  defaultContext: number;
  defaultOutput: number;
}>;

export function ModelSettings({ catalog, onChange, defaultContext, defaultOutput }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [label, setLabel] = useState("");
  const [provider, setProvider] = useState("");
  const [key, setKey] = useState("");
  const [replaceKey, setReplaceKey] = useState("");
  const [modelLabel, setModelLabel] = useState("");
  const [modelId, setModelId] = useState("");
  const [credential, setCredential] = useState("");
  const [context, setContext] = useState(defaultContext);
  const [output, setOutput] = useState(defaultOutput);
  const selectedProvider = provider || catalog?.providers[0]?.id || "";

  async function request(path: string, payload?: unknown) {
    setBusy(true); setError(""); setNotice("");
    try {
      const updated = await apiJson<ModelCatalog>(path, { method: "POST", ...(payload ? { body: JSON.stringify(payload) } : {}) });
      onChange(validateModelCatalog(updated));
      return updated;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Model configuration could not be saved.");
      return null;
    } finally { setBusy(false); }
  }

  async function saveKey(event: FormEvent) {
    event.preventDefault();
    const saved = await request("models/credentials", { label, provider: selectedProvider, api_key: key, credential_id: replaceKey || null });
    setKey("");
    if (saved) {
      const connection = saved.credentials.find((item) => replaceKey ? item.id === replaceKey : item.label === label.trim() && item.provider === selectedProvider);
      if (connection) setCredential(connection.id);
      setLabel(""); setReplaceKey(""); setNotice("API key saved. Map a model to this key below; configuration has been refreshed.");
    }
  }

  async function saveModel(event: FormEvent) {
    event.preventDefault();
    if (await request("models/profiles", { label: modelLabel, model_id: modelId, credential_id: credential || catalog?.credentials[0]?.id, context_window_tokens: context, max_output_tokens: output })) {
      setModelLabel(""); setModelId(""); setNotice("Model saved. Choose it beside the microphone in chat.");
    }
  }

  return <section className="model-settings" aria-label="Models and API keys">
    <div className="settings-card">
      <h3>Models and API keys</h3>
      <button type="button" disabled={busy} onClick={() => void request("models/refresh").then((ok) => { if (ok) setNotice("Saved model configuration and API keys refreshed."); })}>{busy ? "Working…" : "Refresh model configuration"}</button>
      {error && <p role="alert" className="form-error">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      {!catalog && <p>Model configuration is unavailable. Use Refresh to retry.</p>}
      {catalog && <ul className="model-profile-list">{catalog.models.map((model) => <li key={model.id}><strong>{model.label}</strong><span>{model.provider} · {catalog.credentials.find((item) => item.id === model.credential_id)?.label ?? "Configured key"}</span></li>)}</ul>}
    </div>
    <form className="settings-card model-form" onSubmit={(event) => void saveKey(event)}>
      <h3>Add or replace an API key</h3>
      <label>Saved key<select disabled={busy} value={replaceKey} onChange={(event) => {
        setReplaceKey(event.target.value); setKey("");
        const found = catalog?.credentials.find((item) => item.id === event.target.value);
        setLabel(found?.label ?? ""); setProvider(found?.provider ?? "");
      }}><option value="">Add a new key</option>{catalog?.credentials.filter((item) => !item.id.startsWith("env:")).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
      <label>Key label<input autoComplete="off" required maxLength={100} disabled={busy} value={label} onChange={(event) => setLabel(event.target.value)} placeholder="Personal account" /></label>
      <label>Provider<select required disabled={busy || !!replaceKey} value={selectedProvider} onChange={(event) => setProvider(event.target.value)}>{catalog?.providers.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
      <label>API key<input type="password" autoComplete="new-password" spellCheck={false} required maxLength={4096} disabled={busy} value={key} onChange={(event) => setKey(event.target.value)} /></label>
      <button className="primary-button" disabled={busy || !catalog} type="submit">{replaceKey ? "Replace API key" : "Save API key"}</button>
    </form>
    <form className="settings-card model-form" onSubmit={(event) => void saveModel(event)}>
      <h3>Add a model</h3>
      <label>Model label<input required maxLength={100} disabled={busy} value={modelLabel} onChange={(event) => setModelLabel(event.target.value)} placeholder="Everyday assistant" /></label>
      <label>Provider model ID<input required maxLength={200} disabled={busy} value={modelId} onChange={(event) => setModelId(event.target.value)} placeholder="Exact model ID from your provider" /></label>
      <label>Use API key<select required disabled={busy || !catalog?.credentials.length} value={credential || catalog?.credentials[0]?.id || ""} onChange={(event) => setCredential(event.target.value)}>{catalog?.credentials.map((item) => <option key={item.id} value={item.id}>{item.label} · {item.provider}</option>)}</select></label>
      <div className="model-limits"><label>Context window (tokens)<input type="number" required min={256} max={2000000} disabled={busy} value={context} onChange={(event) => setContext(Number(event.target.value))} /></label><label>Maximum output (tokens)<input type="number" required min={1} max={2000000} disabled={busy} value={output} onChange={(event) => setOutput(Number(event.target.value))} /></label></div>
      <button className="primary-button" disabled={busy || !catalog?.credentials.length} type="submit">Add model</button>
    </form>
  </section>;
}
