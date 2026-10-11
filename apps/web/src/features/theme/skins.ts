export const AGENT_SKINS = [
  { id: "sage", name: "Sage", description: "Quiet greens and warm ivory", light: "#29634a", dark: "#a4c9a8" },
  { id: "sand", name: "Sand", description: "Earth tones and soft linen", light: "#805331", dark: "#e0bc91" },
  { id: "rose", name: "Rose", description: "Dusty rose and porcelain", light: "#963f53", dark: "#e7abb8" },
  { id: "ocean", name: "Ocean", description: "Deep teal and sea glass", light: "#146373", dark: "#92cbd1" },
  { id: "graphite", name: "Graphite", description: "Charcoal and clean neutrals", light: "#47545b", dark: "#bdc7cb" },
] as const;
export type AgentSkin = (typeof AGENT_SKINS)[number]["id"];
export function parseSkin(value: string | null): AgentSkin {
  return AGENT_SKINS.find((skin) => skin.id === value)?.id ?? "sage";
}
