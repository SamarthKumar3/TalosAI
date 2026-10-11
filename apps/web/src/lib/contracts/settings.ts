export type AgentSummary = {
  name: string;
  provider: string;
  model_alias: string;
  model: string;
  pipeline: string;
  configuration_version: number;
  configuration_hash: string;
};

export type GuardrailSettings = {
  block_message: string;
  custom_keywords: string[];
  custom_profanity: string[];
  custom_secrets: { id: string; fingerprint: string }[];
  immutable: { id: string; label: string; detail: string; rule_count: number }[];
};

export type RuntimeFeatureSettings = {
  configuration_version: number;
  advanced_rag: {
    enabled: boolean;
    query_rewriting: boolean;
    query_expansion: boolean;
    dense_retrieval: boolean;
    sparse_retrieval: boolean;
    reranking: boolean;
  };
  memory: {
    enabled: boolean;
    short_term: boolean;
    summarization: boolean;
    long_term: boolean;
  };
};
