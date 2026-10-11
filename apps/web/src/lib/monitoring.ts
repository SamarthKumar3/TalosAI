import type { Conversation } from "@/lib/contracts";

export type DailyActivity = {
  dayKey: string;
  label: string;
  conversations: number;
  messages: number;
};

export type WorkspaceMetrics = {
  conversations: number;
  messages: number;
  userMessages: number;
  assistantMessages: number;
};

const DAY_MILLISECONDS = 24 * 60 * 60 * 1000;

export function getWorkspaceMetrics(conversations: Conversation[]): WorkspaceMetrics {
  let messages = 0;
  let userMessages = 0;
  let assistantMessages = 0;

  for (const conversation of conversations) {
    for (const message of conversation.messages) {
      if (message.role === "system") continue;
      messages += 1;
      if (message.role === "user") userMessages += 1;
      if (message.role === "assistant") assistantMessages += 1;
    }
  }

  return {
    conversations: conversations.length,
    messages,
    userMessages,
    assistantMessages,
  };
}

export function buildDailyActivity(
  conversations: Conversation[],
  now: Date = new Date(),
): DailyActivity[] {
  const end = new Date(now);
  end.setHours(0, 0, 0, 0);

  const activity = Array.from({ length: 7 }, (_, index) => {
    const date = new Date(end.getTime() - (6 - index) * DAY_MILLISECONDS);
    return {
      dayKey: localDayKey(date),
      label: new Intl.DateTimeFormat(undefined, { weekday: "short" }).format(date),
      conversations: 0,
      messages: 0,
    };
  });
  const byDay = new Map(activity.map((item) => [item.dayKey, item]));

  for (const conversation of conversations) {
    const bucket = byDay.get(localDayKey(new Date(conversation.created_at)));
    if (!bucket) continue;
    bucket.conversations += 1;
    bucket.messages += conversation.messages.filter((message) => message.role !== "system").length;
  }

  return activity;
}

function localDayKey(value: Date): string {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}
