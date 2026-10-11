"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { apiJson } from "@/lib/talos-client/api-json";

type Extensions = { tools: string[]; skills: { id: string; enabled: boolean }[] };
type Parameter = { name: string; type: string };

export function ExtensionSettings({ section }: Readonly<{ section: "tools" | "skills" }>) {
  const [data, setData] = useState<Extensions | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [kind, setKind] = useState("template");
  const [body, setBody] = useState("");
  const [parameters, setParameters] = useState<Parameter[]>([{ name: "text", type: "string" }]);
  const [upload, setUpload] = useState("");
  const isTool = section === "tools";
  const refresh = useCallback(async (signal?: AbortSignal) => {
    const result = await apiJson<Extensions>("extensions", { signal });
    setData(result);
    setError("");
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    apiJson<Extensions>("extensions", { signal: controller.signal }).then(setData).catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Could not load extensions.");
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [refresh]);

  async function save(path: string, payload: unknown) {
    setBusy(true); setError(""); setNotice("");
    try {
      await apiJson(path, { method: "POST", body: JSON.stringify(payload) });
      await refresh();
      setNotice("Saved. Start a new conversation to use the updated configuration.");
      return true;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not save. Your input has been retained.");
      return false;
    } finally { setBusy(false); }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (isTool) {
      if (!upload && new Set(parameters.map((item) => item.name)).size !== parameters.length) {
        setError("Parameter names must be unique."); return;
      }
      const content = upload || JSON.stringify({ schema_version: 1, name, description, kind, parameters: Object.fromEntries(parameters.map((item) => [item.name, { type: item.type }])), template: body });
      if (await save("extensions/tools", { content })) { setName(""); setDescription(""); setBody(""); setUpload(""); }
    } else {
      const content = upload || `---\n${JSON.stringify({ schema_version: 1, id: name, version: "1.0.0", name, description })}\n---\n${body}\n`;
      if (await save("extensions/skills/upload", { filename: `${name}.md`, content })) { setName(""); setDescription(""); setBody(""); setUpload(""); }
    }
  }

  return <section className="settings-view extension-settings" aria-label={isTool ? "Tools" : "Skills"}>
    {error && <p role="alert" className="form-error">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    <div className="settings-card">
      <h3>{isTool ? "Available tools" : "Installed skills"}</h3>
      {loading ? <p role="status">Loading…</p> : <ul className="extension-list">{isTool ? data?.tools.map((item) => <li key={item}><code>{item}</code></li>) : data?.skills.map((item) => <li key={item.id}><span>{item.id}</span><button type="button" disabled={busy} aria-label={`${item.enabled ? "Disable" : "Enable"} ${item.id}`} aria-pressed={item.enabled} onClick={() => void save("extensions/skills", { action: item.enabled ? "disable" : "enable", value: item.id })}>{item.enabled ? "Enabled" : "Disabled"}</button></li>)}</ul>}
      {!loading && data && (isTool ? !data.tools.length : !data.skills.length) && <p>No {section} added yet.</p>}
      {!loading && !data && <button type="button" onClick={() => { setLoading(true); refresh().catch((reason: Error) => setError(reason.message)).finally(() => setLoading(false)); }}>Retry</button>}
    </div>
    <form className="settings-card extension-form" onSubmit={(event) => void submit(event)}>
      <h3>Add {isTool ? "a tool" : "a skill"}</h3>
      <label>Import {isTool ? "YAML" : "Markdown"} (optional)<input type="file" accept={isTool ? ".yaml,.yml" : ".md"} disabled={busy} onChange={async (event) => {
        const file = event.target.files?.[0];
        if (!file) return;
        if (file.size > 32768) { setError("Files must be at most 32 KiB."); event.target.value = ""; return; }
        try { setUpload(await file.text()); if (!isTool) setName(file.name.replace(/\.md$/i, "").replace(/[^A-Za-z0-9_-]/g, "_")); setError(""); }
        catch { setError("Could not read this file."); }
      }} /></label>
      {upload ? <><label>Review imported content<textarea rows={10} value={upload} disabled={busy} onChange={(event) => setUpload(event.target.value)} /></label><button type="button" disabled={busy} onClick={() => setUpload("")}>Use the visual form</button>{!isTool && <label>Filename<input required pattern="[A-Za-z0-9_-]+" value={name} disabled={busy} onChange={(event) => setName(event.target.value)} /></label>}</> : <>
        <label>{isTool ? "Tool name" : "Skill ID"}<input required pattern="[A-Za-z_][A-Za-z0-9_]{0,63}" maxLength={64} placeholder={isTool ? "format_summary" : "code_review"} value={name} disabled={busy} onChange={(event) => setName(event.target.value)} /></label>
        <label>Description<input required maxLength={1000} value={description} disabled={busy} onChange={(event) => setDescription(event.target.value)} /></label>
        {isTool && <><label>Operation<select value={kind} disabled={busy} onChange={(event) => { setKind(event.target.value); setParameters(event.target.value === "calculator" ? [{ name: "amount", type: "number" }] : [{ name: "text", type: "string" }]); setBody(""); }}><option value="template">Text formatter</option><option value="calculator">Calculator</option></select></label>
          <fieldset disabled={busy}><legend>Input parameters</legend>{parameters.map((item, index) => <div className="extension-parameter" key={index}>
            <input aria-label={`Parameter ${index + 1} name`} required pattern="[A-Za-z_][A-Za-z0-9_]{0,63}" value={item.name} onChange={(event) => setParameters(parameters.map((value, at) => at === index ? { ...value, name: event.target.value } : value))} />
            <select aria-label={`Parameter ${index + 1} type`} value={item.type} onChange={(event) => setParameters(parameters.map((value, at) => at === index ? { ...value, type: event.target.value } : value))}>{(kind === "calculator" ? ["number", "integer"] : ["string", "number", "integer", "boolean"]).map((type) => <option key={type}>{type}</option>)}</select>
            <button type="button" aria-label={`Remove parameter ${index + 1}`} onClick={() => setParameters(parameters.filter((_, at) => at !== index))}>Remove</button>
          </div>)}<button type="button" disabled={parameters.length >= 16} onClick={() => setParameters([...parameters, { name: "", type: kind === "calculator" ? "number" : "string" }])}>Add parameter</button></fieldset></>}
        <label>{isTool ? kind === "calculator" ? "Arithmetic expression" : "Output template" : "Instructions"}<textarea required rows={6} maxLength={isTool ? 4000 : 28000} placeholder={isTool ? kind === "calculator" ? "{amount} * 1.18" : "Summary: {text}" : "When reviewing code, explain the issue and suggest a focused fix."} value={body} disabled={busy} onChange={(event) => setBody(event.target.value)} /></label>
      </>}
      <button className="primary-button" type="submit" disabled={busy || loading || !data}>{busy ? "Saving…" : isTool ? "Add tool" : "Add skill"}</button>
    </form>
  </section>;
}
