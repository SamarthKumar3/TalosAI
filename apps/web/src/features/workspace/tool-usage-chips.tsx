"use client";
import { useId, useState } from "react";
import type { ToolUsage } from "@/lib/contracts/conversation";
import { ToolIcon } from "@/components/icons";

function ToolChip({ tool }: { tool: ToolUsage }) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const label = `${tool.status === "completed" ? "Tool used" : "Tool failed"}: ${tool.name}`;
  return <span className={`tool-usage-chip ${open ? "is-open" : ""} ${dismissed ? "is-dismissed" : ""}`}>
    <button type="button" className={`tool-usage-icon tool-usage-${tool.status}`} aria-label={label} aria-describedby={id} aria-expanded={open} onFocus={() => setDismissed(false)} onMouseEnter={() => setDismissed(false)} onClick={() => { setDismissed(false); setOpen(!open); }} onKeyDown={(event) => { if (event.key === "Escape") { setOpen(false); setDismissed(true); } }}><ToolIcon /></button>
    <span id={id} className="tool-usage-tooltip" role="tooltip">{tool.name}</span>
  </span>;
}
export function ToolUsageChips({ tools }: { tools: ToolUsage[] }) {
  return <span className="tool-usage-chips">{tools.map((tool) => <ToolChip key={`${tool.name}-${tool.status}`} tool={tool} />)}</span>;
}
