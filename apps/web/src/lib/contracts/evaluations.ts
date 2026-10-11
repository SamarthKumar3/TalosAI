export type EvaluationConfiguration = {
  hash: string;
  version: number;
  provider: string;
  model: string;
  pipeline: string;
  created_at: string;
};

export type EvaluationDataset = {
  id: string;
  name: string;
  version: number;
  description: string;
  content_hash: string;
  case_count: number;
  created_at: string;
};

export type EvaluationVariantSummary = {
  cases: number;
  pass_rate: number;
  failure_rate: number;
  exact_match: number;
  citation_recall: number;
  p50_latency_ms: number;
  p95_latency_ms: number;
  input_tokens: number;
  output_tokens: number;
  estimated_cost: number | null;
};

export type EvaluationRun = {
  id: string;
  dataset_id: string;
  state: "pending" | "processing" | "completed" | "failed" | "cancelled";
  baseline_configuration_hash: string;
  candidate_configuration_hash: string;
  changed_fields: string[];
  evaluator_version: string;
  seed: number;
  result_count: number;
  attempt_count: number;
  error_code: string | null;
  created_at: string;
  completed_at: string | null;
  summary: {
    baseline: EvaluationVariantSummary;
    candidate: EvaluationVariantSummary;
    pass_rate_delta: number;
    gate: { passed: boolean; reasons: string[]; thresholds: Record<string, number> };
  } | null;
};
