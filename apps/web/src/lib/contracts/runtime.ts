export type ContextEstimate = {
  used_tokens: number;
  usable_input_tokens: number;
  context_window_tokens: number;
  reserved_output_tokens: number;
  estimated: boolean;
  includes_retrieval: boolean;
};

export type RuntimeEvent = {
  event_id: string;
  type:
    | "run.started" | "response.delta" | "response.completed" | "usage.recorded"
    | "run.completed" | "run.cancelled" | "run.failed" | "tool.completed"
    | "tool.failed" | "guardrail.blocked" | "retrieval.completed" | "model.retrying"
    | "memory.summary_created" | "memory.compacting" | "run.reconciled" | "span.completed";
  run_id: string;
  conversation_id: string;
  sequence: number;
  schema_version: number;
  occurred_at: string;
  payload: Record<string, unknown>;
};
