export function appendTranscript(draft: string, transcript: string): string {
  const normalized = transcript.trim();
  if (!normalized) return draft;
  if (!draft.trim()) return normalized;
  return `${draft.trimEnd()} ${normalized}`;
}
