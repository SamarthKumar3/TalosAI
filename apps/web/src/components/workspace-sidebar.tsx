import type { Conversation } from "@/lib/contracts";
import { useEffect, useRef } from "react";
import { conversationTitle, formatConversationDate } from "@/features/workspace/workspace-utils";
import {
  BackIcon,
  ChartIcon,
  CloseIcon,
  MessageIcon,
  PlusIcon,
  RuntimeIcon,
  SettingsIcon,
  ShieldIcon,
} from "./icons";

export type SettingsSection =
  | "tools"
  | "skills"
  | "appearance"
  | "runtime"
  | "debug"
  | "rag"
  | "memory"
  | "guardrails"
  | "monitoring"
  | "traces"
  | "evaluations";

type WorkspaceSidebarProps = Readonly<{
  activeId: string | undefined;
  conversations: Conversation[];
  loading: boolean;
  modal: boolean;
  navigationOpen: boolean;
  onConversationSelect: (conversation: Conversation) => Promise<void>;
  onNavigationClose: () => void;
  onNewConversation: () => Promise<void>;
  onSettingsSectionSelect: (section: SettingsSection) => void;
  onSettingsToggle: () => void;
  settingsOpen: boolean;
  settingsSection: SettingsSection;
  streaming: boolean;
}>;

export function WorkspaceSidebar({
  activeId,
  conversations,
  loading,
  modal,
  navigationOpen,
  onConversationSelect,
  onNavigationClose,
  onNewConversation,
  onSettingsSectionSelect,
  onSettingsToggle,
  settingsOpen,
  settingsSection,
  streaming,
}: WorkspaceSidebarProps) {
  const sidebar = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!modal) return;
    const previous = document.activeElement as HTMLElement | null;
    sidebar.current?.querySelector<HTMLButtonElement>("button")?.focus();
    return () => previous?.focus();
  }, [modal]);
  let historyContent;
  if (loading) {
    historyContent = (
      <div aria-label="Loading conversations" className="history-skeleton">
        <span />
        <span />
        <span />
      </div>
    );
  } else if (conversations.length === 0) {
    historyContent = <p className="sidebar-empty">Your conversations will appear here.</p>;
  } else {
    historyContent = conversations.map((conversation) => (
      <button
        aria-current={activeId === conversation.id ? "page" : undefined}
        className="history-item"
        disabled={streaming}
        key={conversation.id}
        onClick={() => void onConversationSelect(conversation)}
        type="button"
      >
        <MessageIcon />
        <span>
          <strong>{conversationTitle(conversation)}</strong>
          <small>{formatConversationDate(conversation.created_at)}</small>
        </span>
      </button>
    ));
  }
  return (
    <aside ref={sidebar} role={modal ? "dialog" : undefined} aria-modal={modal || undefined} aria-label="Workspace navigation" className={`sidebar ${navigationOpen ? "is-open" : ""}`} onKeyDown={(event) => {
      if (!modal) return;
      if (event.key === "Escape") { event.preventDefault(); onNavigationClose(); }
      if (event.key !== "Tab") return;
      const controls = [...(sidebar.current?.querySelectorAll<HTMLElement>("button:not([disabled]), a[href]") ?? [])].filter((element) => element.getClientRects().length > 0);
      const first = controls[0];
      const last = controls.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }}>
        <div className="brand-row">
          <div className="brand-mark" aria-hidden="true">T</div>
          <div>
            <p className="brand-name">TalosAI</p>
            {!settingsOpen && <p className="brand-caption">Local workspace</p>}
          </div>
          <button
            aria-label="Close conversation navigation"
            className="icon-button sidebar-close"
            onClick={() => onNavigationClose()}
            type="button"
          >
            <CloseIcon />
          </button>
        </div>

        {settingsOpen && (
          <div className="settings-sidebar-body">
            <div className="settings-sidebar-heading">
              <h2>Settings</h2>
            </div>
            <nav aria-label="Settings topics" className="settings-navigation">
              <button aria-current={settingsSection === "tools" ? "page" : undefined} className="settings-nav-item" onClick={() => onSettingsSectionSelect("tools")} type="button">
                <RuntimeIcon /><span><strong>Tools</strong></span>
              </button>
              <button aria-current={settingsSection === "skills" ? "page" : undefined} className="settings-nav-item" onClick={() => onSettingsSectionSelect("skills")} type="button">
                <MessageIcon /><span><strong>Skills</strong></span>
              </button>
              <button aria-current={settingsSection === "rag" ? "page" : undefined} className="settings-nav-item" onClick={() => onSettingsSectionSelect("rag")} type="button">
                <ChartIcon />
                <span><strong>Advanced RAG</strong></span>
              </button>
              <button aria-current={settingsSection === "memory" ? "page" : undefined} className="settings-nav-item" onClick={() => onSettingsSectionSelect("memory")} type="button">
                <MessageIcon />
                <span><strong>Memory</strong></span>
              </button>
              <button aria-current={settingsSection === "guardrails" ? "page" : undefined} className="settings-nav-item" onClick={() => onSettingsSectionSelect("guardrails")} type="button">
                <ShieldIcon />
                <span><strong>Guardrails</strong></span>
              </button>
              <button aria-current={settingsSection === "monitoring" ? "page" : undefined} className="settings-nav-item" onClick={() => onSettingsSectionSelect("monitoring")} type="button">
                <ChartIcon />
                <span><strong>Monitoring</strong></span>
              </button>
              <button aria-current={settingsSection === "traces" ? "page" : undefined} className="settings-nav-item" onClick={() => onSettingsSectionSelect("traces")} type="button">
                <RuntimeIcon /><span><strong>Traces</strong></span>
              </button>
              <button aria-current={settingsSection === "evaluations" ? "page" : undefined} className="settings-nav-item" onClick={() => onSettingsSectionSelect("evaluations")} type="button">
                <ChartIcon />
                <span><strong>Evaluations</strong></span>
              </button>
              <button aria-current={settingsSection === "appearance" ? "page" : undefined} className="settings-nav-item" onClick={() => onSettingsSectionSelect("appearance")} type="button">
                <SettingsIcon />
                <span><strong>Appearance</strong></span>
              </button>
              <button aria-current={settingsSection === "runtime" ? "page" : undefined} className="settings-nav-item" onClick={() => onSettingsSectionSelect("runtime")} type="button">
                <RuntimeIcon />
                <span><strong>Runtime</strong></span>
              </button>
              <button aria-current={settingsSection === "debug" ? "page" : undefined} className="settings-nav-item" onClick={() => onSettingsSectionSelect("debug")} type="button">
                <RuntimeIcon /><span><strong>Debug</strong></span>
              </button>
            </nav>
          </div>
        )}

        {!settingsOpen && (
          <button
            className="new-chat-button"
            disabled={streaming}
            onClick={() => void onNewConversation()}
            type="button"
          >
            <PlusIcon /> New conversation
          </button>
        )}
        <nav aria-label="Conversation history" className="history" hidden={settingsOpen}>
          <p className="section-label">Recent</p>
          {historyContent}
        </nav>

        <button aria-pressed={settingsOpen} className={`settings-button ${settingsOpen ? "is-active" : ""}`} onClick={onSettingsToggle} type="button">
          {settingsOpen ? <BackIcon /> : <SettingsIcon />}
          {settingsOpen ? "Back to conversations" : "Settings"}
        </button>
    </aside>
  );
}
