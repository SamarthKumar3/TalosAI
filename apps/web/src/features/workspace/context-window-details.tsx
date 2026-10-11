"use client";

import { type CSSProperties, useEffect, useId, useRef, useState } from "react";

import { CloseIcon } from "@/components/icons";
import type { ContextEstimate } from "@/lib/contracts";

const compactNumber = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

function TokenCount({ value }: { value: number }) {
  return <span className="context-token-count" tabIndex={0} aria-label={`${value.toLocaleString("en-US")} tokens`}>
    {compactNumber.format(value)}
    <span className="context-number-tooltip" aria-hidden="true">{value.toLocaleString("en-US")} tokens</span>
  </span>;
}

export function ContextWindowDetails({ estimate }: { estimate: ContextEstimate }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const id = useId();
  const [infoOpen, setInfoOpen] = useState(false);
  const remaining = Math.max(0, estimate.usable_input_tokens - estimate.used_tokens);
  const overBudget = estimate.used_tokens > estimate.usable_input_tokens;
  const percent = Math.round(100 * estimate.used_tokens / Math.max(1, estimate.usable_input_tokens));
  const numbers = [
    ["Estimated input", estimate.used_tokens],
    ["Remaining input budget", remaining],
    ["Usable input limit", estimate.usable_input_tokens],
    ["Reserved for output", estimate.reserved_output_tokens],
    ["Model context window", estimate.context_window_tokens],
  ] as const;

  useEffect(() => {
    const dialog = dialogRef.current;
    return () => dialog?.close();
  }, []);

  return <>
    <button
      aria-label="Open context window details"
      aria-haspopup="dialog"
      aria-controls={`${id}-dialog`}
      className="context-window-button"
      title={`Estimated context: ${estimate.used_tokens.toLocaleString()} / ${estimate.usable_input_tokens.toLocaleString()} usable input tokens`}
      type="button"
      onClick={() => {
        dialogRef.current?.showModal();
        closeRef.current?.focus();
      }}
    >
      <span aria-hidden="true" className="context-window-indicator" style={{ "--context-percent": `${Math.min(100, percent)}%` } as CSSProperties}><i /></span>
    </button>
    <dialog
      aria-labelledby={`${id}-title`}
      aria-describedby={`${id}-description`}
      className="context-window-dialog"
      id={`${id}-dialog`}
      ref={dialogRef}
      onClose={() => setInfoOpen(false)}
      onCancel={(event) => {
        if (infoOpen) { event.preventDefault(); setInfoOpen(false); }
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape" && !infoOpen) {
          event.preventDefault();
          dialogRef.current?.close();
        }
      }}
      onClick={(event) => {
        if (event.target !== event.currentTarget) return;
        const bounds = event.currentTarget.getBoundingClientRect();
        if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) event.currentTarget.close();
      }}
    >
      <header>
        <div className="context-window-heading">
          <h2 id={`${id}-title`}>Context window details</h2>
          <button aria-label="About context estimates" aria-expanded={infoOpen} aria-controls={`${id}-info`} className="context-info-button" type="button" onClick={() => setInfoOpen(!infoOpen)}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="M12 11v6" /><circle cx="12" cy="7" r=".8" fill="currentColor" stroke="none" /></svg>
          </button>
        </div>
        <button aria-label="Close context window details" className="icon-button" onClick={() => dialogRef.current?.close()} ref={closeRef} type="button"><CloseIcon /></button>
      </header>
      <div className="context-estimate-info" id={`${id}-info`} hidden={!infoOpen}>
        <p>Includes system instructions, enabled skills, tool definitions, your draft, and conversation history when memory is enabled. The usable input limit accounts for output reservations, safety margins, and per-run limits.</p>
        {!estimate.includes_retrieval && <p>Retrieved passages are not included in this estimate. Retrieval may add context after you send the message.</p>}
      </div>
      <p id={`${id}-description`}>A conservative estimate for the next request. Final provider token usage may differ.</p>
      <div className="context-budget-summary"><strong>{percent.toLocaleString()}%</strong><span>of the usable input budget</span></div>
      <div aria-hidden="true" className="context-budget-bar"><span style={{ width: `${Math.min(100, percent)}%` }} /></div>
      <dl className="context-window-metrics">{numbers.map(([label, value]) => <div key={label}><dt>{label}</dt><dd><TokenCount value={value} /> <span className="context-token-unit">tokens</span></dd></div>)}</dl>
      {overBudget && <p className="context-budget-warning">Estimated input exceeds the usable budget by <TokenCount value={estimate.used_tokens - estimate.usable_input_tokens} /> tokens. Shorten your draft or start a new conversation if it cannot fit after automatic compaction.</p>}
    </dialog>
  </>;
}
