import type { RuntimeEvent } from "./contracts";

export type SseFrame = {
  event?: string;
  id?: string;
  data?: string;
};

export const MAX_SSE_BUFFER_BYTES = 1_000_000;

export function parseSseFrames(value: string): { frames: SseFrame[]; remainder: string } {
  if (value.length > MAX_SSE_BUFFER_BYTES) {
    throw new Error("Talos event stream exceeded the safe buffer limit.");
  }
  const normalized = value.replaceAll("\r\n", "\n");
  const chunks = normalized.split("\n\n");
  const remainder = chunks.pop() ?? "";
  const frames = chunks.map(parseFrame).filter((frame): frame is SseFrame => frame !== null);
  return { frames, remainder };
}

function parseFrame(chunk: string): SseFrame | null {
  const frame: SseFrame = {};
  for (const line of chunk.split("\n")) applyField(frame, line);
  return frame.data ? frame : null;
}

function applyField(frame: SseFrame, line: string): void {
  if (!line || line.startsWith(":")) return;
  const separator = line.indexOf(":");
  const field = separator < 0 ? line : line.slice(0, separator);
  const value = separator < 0 ? "" : line.slice(separator + 1).trimStart();
  if (field === "event") frame.event = value;
  if (field === "id") frame.id = value;
  if (field === "data") {
    const dataPrefix = frame.data ? `${frame.data}\n` : "";
    frame.data = `${dataPrefix}${value}`;
  }
}

export function runtimeEventFromFrame(frame: SseFrame): RuntimeEvent {
  if (!frame.data) throw new Error("SSE frame is missing data");
  const value: unknown = JSON.parse(frame.data);
  if (
    typeof value !== "object" ||
    value === null ||
    !("type" in value) ||
    typeof value.type !== "string" ||
    !("run_id" in value) ||
    typeof value.run_id !== "string" ||
    !("conversation_id" in value) ||
    typeof value.conversation_id !== "string" ||
    !("event_id" in value) ||
    typeof value.event_id !== "string" ||
    !("sequence" in value) ||
    typeof value.sequence !== "number" ||
    !Number.isSafeInteger(value.sequence) ||
    !("payload" in value) ||
    typeof value.payload !== "object" ||
    value.payload === null
  ) {
    throw new Error("Talos returned an invalid runtime event");
  }
  return value as RuntimeEvent;
}
