"use client";

import ReactMarkdown from "react-markdown";

import { BookIcon, ChevronIcon } from "../../components/icons";
import { splitAssistantSources } from "./assistant-sources";
import type { ToolUsage } from "@/lib/contracts/conversation";
import { ToolUsageChips } from "./tool-usage-chips";
import { MermaidDownload, RepositorySources } from "./repository-sources";

export function AssistantMessage({ content, tools = [] }: { content: string; tools?: ToolUsage[] }) {
  const parsed = splitAssistantSources(content);
  return (
    <>
      <div className={`assistant-response-body ${tools.length ? "has-tools" : ""}`}><div className="markdown-response"><ReactMarkdown>{parsed.body}</ReactMarkdown></div>{tools.length > 0 && <ToolUsageChips tools={tools} />}</div>
      <RepositorySources sources={parsed.sources} />
      <MermaidDownload content={parsed.body} />
      {parsed.sources.length > 0 && (
        <details className="message-sources">
          <summary><BookIcon /><span>Sources</span><span className="source-count">{parsed.sources.length}</span><ChevronIcon className="source-chevron" /></summary>
          <ol>
            {parsed.sources.map((source) => (
              <li key={`${source.marker}-${source.name}-${source.location}`}>
                <span className="source-marker">{source.marker}</span>
                <span className="source-reference"><strong>{source.name}</strong><small>{source.location}{source.detail ? ` · ${source.detail}` : ""}</small></span>
              </li>
            ))}
          </ol>
        </details>
      )}
    </>
  );
}
