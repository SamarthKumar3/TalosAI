export type TraceSpan = {
  name: string;
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  started_at: string;
  duration_ms: number;
  message_count: number;
  status: string;
  attributes: Record<string, unknown>;
};

export type RunTrace = {
  trace_id: string;
  run_id: string;
  conversation_id: string;
  configuration_version: number;
  configuration_hash: string;
  provider: string;
  model: string;
  state: "running" | "completed" | "cancelled" | "failed";
  started_at: string;
  finished_at: string | null;
  duration_ms: number;
  spans: TraceSpan[];
  usage: {
    model_calls: number;
    input_tokens: number;
    output_tokens: number;
    embedding_requests: number;
    estimated_cost: number | null;
    currency: string | null;
    rate_version: string | null;
    rate_observed_on: string | null;
    cost_status: "estimated" | "unknown";
  };
  retrieval_count: number;
  tool_calls: number;
  retries: number;
  cancelled: boolean;
  error: { code: string; message: string; retryable: boolean } | null;
};

export type UsageReport = {
  runs: number;
  model_calls: number;
  input_tokens: number;
  output_tokens: number;
  embedding_requests: number;
  tool_executions: number;
  retrieval_requests: number;
  estimated_cost: number | null;
  currency: string | null;
  cost_status: "estimated" | "unknown";
  rate_versions: string[];
  summary: {
    sessions: number;
    messages: number;
    total_tokens: number;
    average_latency_ms: number;
    average_cost_per_call: number | null;
    daily_average_cost: number | null;
    daily_average_tokens: number;
    average_tokens_per_call: number;
    peak_usage_date: string | null;
    peak_usage_tokens: number;
  };
  daily: Array<{
    date: string;
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    cost: number;
    messages: number;
    average_latency_ms: number;
    p90_latency_ms: number;
    p95_latency_ms: number;
    p99_latency_ms: number;
  }>;
  statuses: Array<{ name: string; value: number }>;
  models: Array<{ name: string; value: number }>;
  usage_series: Array<{ name: string; points: Array<{ date: string; value: number }> }>;
  filter_options: { models: string[]; providers: string[]; statuses: string[] };
};

export type TracePage = {
  traces: RunTrace[];
  pagination: {
    page: number;
    page_size: 15;
    total_items: number;
    total_pages: number;
    has_previous: boolean;
    has_next: boolean;
  };
};

export type ObservabilityAlert = {
  code: string;
  severity: "warning" | "critical";
  message: string;
  runbook: string;
};
