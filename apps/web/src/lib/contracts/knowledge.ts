export type KnowledgeJobCreated = { job_id: string; status_url: string };

export type KnowledgeJobStatus = {
  id: string;
  source_id: string;
  state: "pending" | "processing" | "completed" | "failed" | "cancelled";
  error_code: string | null;
};

export type KnowledgePreview = {
  source_id: string;
  name: string;
  content: string;
  truncated: boolean;
};
