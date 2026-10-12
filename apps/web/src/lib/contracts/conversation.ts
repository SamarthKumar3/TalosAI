export type MessageAttachment = {
  source_id: string;
  name: string;
};

export type ToolUsage = { name: string; status: "completed" | "failed" };

export type Message = {
  tool_usages?: ToolUsage[];
  id: string;
  run_id?: string | null;
  role: "system" | "user" | "assistant" | "notice";
  content: string;
  created_at: string;
  attachments: MessageAttachment[];
};

export type Conversation = {
  repository?: { id: string; name: string } | null;
  title?: string;
  model_profile_id?: string | null;
  model_id?: string | null;
  provider?: string | null;
  id: string;
  configuration_version: number;
  created_at: string;
  messages: Message[];
};
