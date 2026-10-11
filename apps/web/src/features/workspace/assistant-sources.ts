export type DisplaySource = {
  marker: string;
  name: string;
  location: string;
  detail: string | null;
};

export function splitAssistantSources(content: string): {
  body: string;
  sources: DisplaySource[];
} {
  const separator = "\n\nSources:\n";
  const index = content.lastIndexOf(separator);
  if (index < 0) return { body: content, sources: [] };
  const lines = content.slice(index + separator.length).split("\n").filter(Boolean);
  const sources = lines.map(parseSource).filter((item): item is DisplaySource => item !== null);
  return sources.length > 0 ? { body: content.slice(0, index), sources } : { body: content, sources: [] };
}

function parseSource(line: string): DisplaySource | null {
  const match = /^- \[([^\]]+)] (.+)$/.exec(line.trim());
  if (!match) return null;
  const marker = match[1];
  const reference = match[2];
  const page = /^(.*):page (\d+)$/.exec(reference);
  if (page) return { marker, name: page[1], location: `Page ${page[2]}`, detail: null };
  const lines = /^(.*):(\d+)-(\d+)(?: \((.*)\))?$/.exec(reference);
  if (lines) {
    return {
      marker,
      name: lines[1],
      location: `Lines ${lines[2]}–${lines[3]}`,
      detail: lines[4] ?? null,
    };
  }
  return { marker, name: reference, location: "Referenced excerpt", detail: null };
}
