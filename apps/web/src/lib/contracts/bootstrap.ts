import type { AgentSummary } from "./settings";
import type { Conversation } from "./conversation";

export type UiBootstrap = {
  agent: AgentSummary;
  conversations: Conversation[];
  config: Record<string, unknown>;
};
