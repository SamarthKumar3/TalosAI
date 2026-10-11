import type { Conversation } from "@/lib/contracts";

export function conversationTitle(conversation: Conversation): string {
  const firstPrompt = conversation.messages.find((message) => message.role === "user")?.content;
  const value = conversation.title ?? firstPrompt;
  if (!value?.trim()) return "New conversation";
  // Inspect the entire value before shortening, including optimistic and legacy history.
  if (/\b(?:sk[-*]|gh[pousr]_|github_pat_|AIza|AKIA|ASIA)\S*|\b(?:api[_ -]?key|access[_ -]?token|secret|password|passwd|pwd|bearer)\b|-----BEGIN|\beyJ[A-Za-z0-9_-]+\.|\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b|\b[A-Za-z0-9_/-]{40,}\b|[•*]{4,}/i.test(value)) return "Private conversation";
  const safe = value.replaceAll(/\s+/g, " ").trim();
  return safe.length > 48 ? `${safe.slice(0, 48)}...` : safe;
}

export function formatConversationDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(value));
}

export async function knowledgeContent(file: File): Promise<string> {
  if (!file.name.toLowerCase().endsWith(".pdf")) return file.text();
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCodePoint(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

export function wait(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}
