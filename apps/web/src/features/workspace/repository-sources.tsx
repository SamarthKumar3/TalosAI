"use client";

import { useId } from "react";
import { BookIcon, ReactFileIcon } from "@/components/icons";
import type { DisplaySource } from "./assistant-sources";

export function RepositorySources({ sources }: Readonly<{ sources: DisplaySource[] }>) {
  const prefix = useId();
  const files = sources.filter((source) => /^R\d+$/.test(source.marker));
  if (!files.length) return null;
  return <div className="repository-source-chips" aria-label="Repository source files">{files.map((source) => {
    const react = /\.(?:tsx|jsx)$/i.test(source.name);
    const fullName = `${source.name} · ${source.location}${source.detail ? ` · ${source.detail}` : ""}`;
    return <span className="repository-source" key={source.marker}>
      <button type="button" aria-describedby={`${prefix}-${source.marker}`} title={fullName} aria-label={`Source ${source.marker}: ${fullName}`}>
        {react ? <ReactFileIcon /> : <BookIcon />}<span className="repository-file-name">{source.name.split("/").at(-1)}</span><small>[{source.marker}]</small>
      </button>
      <span className="repository-source-tooltip" id={`${prefix}-${source.marker}`} role="tooltip">{fullName}</span>
    </span>;
  })}</div>;
}

export function mermaidDownload(content: string): string | null {
  const match = /```mermaid\s*\n([\s\S]*?)```/i.exec(content);
  if (!match) return null;
  const lines = match[1].trim().split("\n").map((line) => line.trim()).filter(Boolean);
  if (!/^(?:flowchart|graph) (?:TD|LR)$/.test(lines[0] ?? "") || lines.length > 150) return null;
  const nodes = new Set<string>();
  const edges: [string, string][] = [];
  for (const line of lines.slice(1)) {
    const node = /^([A-Za-z][A-Za-z0-9_]*)\["([^"<>]{1,120})"\];?$/.exec(line);
    const edge = /^([A-Za-z][A-Za-z0-9_]*) --> ([A-Za-z][A-Za-z0-9_]*);?$/.exec(line);
    if (node && node[1].toLowerCase() !== "end" && !nodes.has(node[1])) nodes.add(node[1]);
    else if (edge) edges.push([edge[1], edge[2]]);
    else return null;
  }
  if (!nodes.size || !edges.length || edges.some(([a, b]) => !nodes.has(a) || !nodes.has(b))) return null;
  // Diagrams are downloaded as text, never executed or rendered in the workspace.
  return match[1].trim() + "\n";
}

export function MermaidDownload({ content }: Readonly<{ content: string }>) {
  const diagram = mermaidDownload(content);
  if (!diagram) return null;
  return <button className="repository-diagram-download" type="button" onClick={() => {
    const url = URL.createObjectURL(new Blob([diagram], { type: "text/plain;charset=utf-8" }));
    const anchor = document.createElement("a");
    anchor.href = url; anchor.download = "repository-hld.mmd";
    anchor.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }}>Download Mermaid diagram (.mmd)</button>;
}
