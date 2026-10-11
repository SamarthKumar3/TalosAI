"use client";

import {
  type ChangeEvent,
  KeyboardEvent,
  type RefObject,
  type ReactNode,
  type SyntheticEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import type {
  AgentSummary,
  Conversation,
  ContextEstimate,
  EvaluationConfiguration,
  EvaluationDataset,
  EvaluationRun,
  GuardrailSettings,
  KnowledgeJobCreated,
  KnowledgeJobStatus,
  KnowledgePreview,
  Message,
  RuntimeEvent,
  RuntimeFeatureSettings,
} from "@/lib/contracts";
import { parseSseFrames, runtimeEventFromFrame } from "@/lib/sse";
import { appendTranscript } from "@/lib/speech-input";
import { apiJson } from "@/lib/talos-client/api-json";
import { FeatureSettingsPanel, GuardrailAdder } from "@/features/settings/settings-controls";
import { useWhyAction, WhyChip, WhyMenu, WhyReportCard, type WhyAction, type WhyReport } from "@/features/workspace/why-action";
import { AssistantMessage } from "@/features/workspace/assistant-message";
import { ContextWindowDetails } from "@/features/workspace/context-window-details";
import { conversationTitle, knowledgeContent, wait } from "@/features/workspace/workspace-utils";
import { ThemeToggle } from "@/features/theme/theme-toggle";
import { useTheme } from "@/features/theme/use-theme";
import { ExtensionSettings } from "@/features/extensions/extension-settings";
import { ChatModelPicker, ModelSettings, validateModelCatalog, type ModelCatalog } from "@/features/settings/model-settings";
import { AppearanceSettings } from "@/features/theme/appearance-settings";
import { DebugSettings } from "@/features/settings/debug-settings";
import { useIsMobile } from "@/hooks/use-is-mobile";
import { MonitoringDashboard } from "@/features/monitoring/monitoring-dashboard";
import { TraceInspector } from "@/features/monitoring/trace-inspector";
import { EvaluationHistory } from "@/features/evaluations/evaluation-history";

import {
  CloseIcon,
  MenuIcon,
  MicrophoneIcon,
  PaperclipIcon,
  PlusIcon,
  RedoIcon,
  SendIcon,
  SettingsIcon,
  StopIcon,
} from "./icons";
import { AttachmentChip, AttachmentPreview, MessageAttachmentCard, type PreviewAttachment, type SelectedAttachment } from "./attachment-preview";
import { ResponseLoading } from "./response-loading";
import { useSpeechInput } from "./use-speech-input";
import { WorkspaceSidebar, type SettingsSection } from "./workspace-sidebar";

type DraftMessage = Message & { failed?: boolean; pending?: boolean };

function ragUploadLimits(config: Record<string, unknown>) {
  const rag = config.rag as { allowed_extensions?: unknown; max_source_bytes?: unknown } | undefined;
  return {
    allowedExtensions: Array.isArray(rag?.allowed_extensions)
      ? rag.allowed_extensions.filter((value): value is string => typeof value === "string")
      : [],
    maxSourceBytes: Number(rag?.max_source_bytes ?? 0),
  };
}

type StreamResult = {
  completed: boolean;
  failure: string | null;
};

function streamEventFailure(event: RuntimeEvent, current: string | null): string | null {
  if (event.type === "run.cancelled") return "Generation stopped.";
  if (event.type !== "run.failed") return current;
  return typeof event.payload.message === "string"
    ? event.payload.message
    : "The Talos run failed. Try again.";
}

async function consumeChatStream(
  response: Response,
  onRuntimeEvent: (event: RuntimeEvent) => void,
): Promise<StreamResult> {
  if (!response.ok || !response.body) {
    throw new Error(`Talos streaming failed with HTTP ${response.status}`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let completed = false;
  let failure: string | null = null;
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const parsed = parseSseFrames(buffer);
    buffer = parsed.remainder;
    for (const frame of parsed.frames) {
      const runtimeEvent = runtimeEventFromFrame(frame);
      completed ||= runtimeEvent.type === "response.completed";
      failure = streamEventFailure(runtimeEvent, failure);
      onRuntimeEvent(runtimeEvent);
    }
    if (done) return { completed, failure };
  }
}

type ConversationContentProps = Readonly<{
  whyReports: WhyReport[];
  active: Conversation | null;
  agent: AgentSummary | null;
  endRef: RefObject<HTMLDivElement | null>;
  loading: boolean;
  messages: DraftMessage[];
  onAttachmentOpen: (sourceId: string) => void;
  onSuggestion: (suggestion: string) => void;
  streaming: boolean;
  usage: { input: number; output: number } | null;
}>;

function messageContent(message: DraftMessage): ReactNode {
  if (!message.content) return message.pending ? <ResponseLoading /> : null;
  return message.role === "assistant"
    ? <AssistantMessage content={message.content} tools={message.tool_usages} />
    : message.content;
}

function ContextCompactionNotice({ message }: Readonly<{ message: DraftMessage }>) {
  const content = <><RedoIcon /><em>{message.pending ? "Compacting context window..." : "Context automatically compacted"}</em></>;
  return message.pending
    ? <output className="context-compaction-notice">{content}</output>
    : <p className="context-compaction-notice">{content}</p>;
}

function ConversationContent({ whyReports, active, agent, endRef, loading, messages, onAttachmentOpen, onSuggestion, streaming, usage }: ConversationContentProps) {
  const timeline: { time: string; message?: DraftMessage; report?: WhyReport }[] = [
    ...messages.filter((message) => message.role !== "system").map((message) => ({
      message, time: message.created_at,
    })),
    ...whyReports.map((report) => ({
      report, time: report.submitted_at ?? report.target.created_at,
    })),
  ];
  timeline.sort((a, b) => a.time.localeCompare(b.time));
  if (loading) {
    return <div className="empty-state"><div className="empty-mark loading-mark" aria-hidden="true">T</div><h1>Connecting to Talos</h1><p>Loading your local workspace and conversation history.</p></div>;
  }
  if (messages.length === 0) {
    const suggestions = ["Explain this project architecture", "Help me plan a safe refactor", "Review an API design"];
    return <div className="empty-state"><div className="empty-mark" aria-hidden="true">T</div><p className="eyebrow">Developer-first intelligence</p><h1>What are we building?</h1><p>Ask about code, architecture, debugging, or the next decision. Your conversation stays pinned to its configuration snapshot.</p><div className="suggestions" aria-label="Suggested prompts">{suggestions.map((suggestion) => <button key={suggestion} onClick={() => onSuggestion(suggestion)} type="button">{suggestion}</button>)}</div></div>;
  }
  return (
    <div className="message-list">
      <div className="snapshot-notice">Conversation pinned to configuration v{active?.configuration_version ?? 1}. Future settings changes apply to new conversations.</div>
      {timeline.map(({ message, report }, index) => report ? (
        <WhyReportCard key={`why-${report.target.message_id}-${report.submitted_at ?? index}`} report={report} />
      ) : message && (
        message.role === "notice" ? (
          <ContextCompactionNotice key={message.id} message={message} />
        ) : (
        <article className={`message message-${message.role} ${message.pending ? "is-streaming" : ""} ${message.failed ? "is-failed" : ""}`} key={message.id} id={`message-${message.id}`} tabIndex={-1}>
          <div className="message-author">{message.role === "assistant" ? agent?.name ?? "Talos" : "You"}</div>
          {message.role === "user" && (message.attachments ?? []).map((item) => <MessageAttachmentCard attachment={item} key={item.source_id} onOpen={() => onAttachmentOpen(item.source_id)} />)}
          <div className="message-content">{messageContent(message)}</div>
          {message.failed && <p className="message-failure">Not sent. Your text is restored in the composer so you can retry.</p>}
        </article>
        )
      ))}
      {usage && !streaming && <p className="usage-line">Last response: {usage.input.toLocaleString()} input / {usage.output.toLocaleString()} output tokens</p>}
      <div ref={endRef} />
    </div>
  );
}

function voiceStatus(speech: ReturnType<typeof useSpeechInput>): string {
  if (speech.error) return speech.error;
  if (speech.listening) return "Listening. Select the microphone again to stop.";
  return speech.supported
    ? "Browser voice recognition may use its provider's service; Talos does not store audio."
    : "Voice input is unavailable in this browser; typing still works.";
}

type ComposerProps = Readonly<{
  why: WhyAction;
  contextEstimate: ContextEstimate | null;
  allowedExtensions: string[];
  attachment: SelectedAttachment | null;
  cancelling: boolean;
  error: string | null;
  fileRef: RefObject<HTMLInputElement | null>;
  onAttachmentOpen: () => void;
  onAttachmentRemove: () => void;
  onCancel: () => void;
  onDismissError: () => void;
  onDocumentSelect: (event: ChangeEvent<HTMLInputElement>) => void;
  onPromptChange: (value: string) => void;
  onPromptKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
  onSubmit: (event: SyntheticEvent<HTMLFormElement, SubmitEvent>) => void;
  prompt: string;
  promptRef: RefObject<HTMLTextAreaElement | null>;
  runId: string | null;
  speech: ReturnType<typeof useSpeechInput>;
  models: ModelCatalog | null;
  selectedModel: string;
  modelChanging: boolean;
  onModelChange: (id: string) => void;
  streaming: boolean;
  uploading: boolean;
  uploadStatus: string | null;
}>;

function Composer({
  allowedExtensions, attachment, cancelling, contextEstimate, error, fileRef, onAttachmentOpen,
  onAttachmentRemove, onCancel, onDismissError, onDocumentSelect, onPromptChange,
  onPromptKeyDown, onSubmit, prompt, promptRef, runId, speech, streaming, uploading,
  uploadStatus, why, models, selectedModel, modelChanging, onModelChange,
}: ComposerProps) {
  const unavailable = streaming || uploading || why.busy || modelChanging;
  const inputDisabled = unavailable || why.active;
  const attachmentsDisabled = inputDisabled || allowedExtensions.length === 0;
  const attachmentTitle = allowedExtensions.length
    ? `Attach for RAG: ${allowedExtensions.join(", ")}`
    : "RAG upload configuration is unavailable";
  return (
    <div className="composer-wrap">
      {error && <div className="error-banner" role="alert"><span>{error}</span><button onClick={onDismissError} type="button">Dismiss</button></div>}
      {why.error && <p role="alert" className="error-banner">{why.error}</p>}
      {why.busy && <p role="status">Loading execution evidence...</p>}
      <form className="composer" onSubmit={onSubmit} onDrop={(event) => { if (inputDisabled) event.preventDefault(); }} onDragOver={(event) => { if (inputDisabled) event.preventDefault(); }}>
        <label className="sr-only" htmlFor="talos-prompt">Message Talos</label>
        <input accept={allowedExtensions.join(",")} className="sr-only" disabled={attachmentsDisabled} onChange={onDocumentSelect} ref={fileRef} tabIndex={-1} type="file" />
        {why.active && <WhyChip action={why} />}
        {!why.active && attachment && <AttachmentChip attachment={attachment} onOpen={onAttachmentOpen} onRemove={onAttachmentRemove} />}
        <div className="composer-input-row"><textarea disabled={inputDisabled} onPaste={(event) => { if (inputDisabled) event.preventDefault(); }} id="talos-prompt" onChange={(event) => onPromptChange(event.target.value)} onKeyDown={onPromptKeyDown} placeholder="Message Talos..." ref={promptRef} rows={1} value={why.active ? "" : prompt} /></div>
        <div className="composer-toolbar">
          <div className="composer-tools">
            <WhyMenu action={why} disabled={unavailable} />
            <button aria-label="Attach a document for RAG" className="attachment-button" disabled={attachmentsDisabled} onClick={() => fileRef.current?.click()} title={attachmentTitle} type="button"><PaperclipIcon /></button>
            <button aria-label={speech.listening ? "Stop voice input" : "Start voice input"} aria-pressed={speech.listening} className={`voice-button ${speech.listening ? "is-listening" : ""}`} disabled={!speech.supported || inputDisabled} onClick={speech.toggle} title={speech.supported ? "Browser speech recognition may send audio to your browser provider" : "Voice input is unavailable in this browser"} type="button"><MicrophoneIcon /></button>
            <ChatModelPicker catalog={models} disabled={inputDisabled || !models || !!attachment} onChange={onModelChange} value={selectedModel} />
          </div>
          <div className="composer-send-group">
          {contextEstimate && <ContextWindowDetails estimate={contextEstimate} />}
          {streaming
            ? <button aria-label="Stop generation" className="send-button stop-button" disabled={!runId || cancelling} onClick={onCancel} type="button"><StopIcon /></button>
            : <button aria-label="Send message" className="send-button" disabled={modelChanging || (why.active ? why.busy || !why.target : !prompt.trim() || uploading || why.busy)} type="submit"><SendIcon /></button>}
          </div>
        </div>
      </form>
      <p aria-live="polite" className="voice-status">{voiceStatus(speech)}</p>
      <p aria-live="polite" className="upload-status">{uploadStatus}</p>
    </div>
  );
}

type WorkspaceTopbarProps = Readonly<{
  active: Conversation | null;
  agent: AgentSummary | null;
  onNavigationOpen: () => void;
  onSettingsOpen: () => void;
  onThemeToggle: () => void;
  theme: ReturnType<typeof useTheme>["theme"];
}>;

function WorkspaceTopbar({ active, agent, onNavigationOpen, onSettingsOpen, onThemeToggle, theme }: WorkspaceTopbarProps) {
  return (
    <header className="topbar">
      <button aria-label="Open conversation navigation" className="icon-button mobile-menu" onClick={onNavigationOpen} type="button"><MenuIcon /></button>
      <div className="topbar-title">
        <strong>{active ? conversationTitle(active) : "New conversation"}</strong>
        <span><i aria-hidden="true" className="status-dot" />{active?.model_id ? `${active.provider} / ${active.model_id}` : agent ? `${agent.provider} / ${agent.model}` : "Connecting to runtime"}</span>
      </div>
      {active && <span className="snapshot-badge" title="This conversation keeps its original configuration">Config v{active.configuration_version}</span>}
      <ThemeToggle onToggle={onThemeToggle} theme={theme} />
      <button aria-label="Open settings" className="icon-button mobile-settings" onClick={onSettingsOpen} type="button"><SettingsIcon /></button>
    </header>
  );
}

type RuntimeFeaturePanelsProps = Readonly<{
  features: RuntimeFeatureSettings | null;
  onSave: (next: RuntimeFeatureSettings) => void;
  saving: boolean;
  section: SettingsSection;
  status: string | null;
}>;

function RuntimeFeaturePanels({ features, onSave, saving, section, status }: RuntimeFeaturePanelsProps) {
  const advancedOptions = features ? [
    ["Query rewriting", "query_rewriting", features.advanced_rag.query_rewriting],
    ["Query expansion", "query_expansion", features.advanced_rag.query_expansion],
    ["Dense retrieval", "dense_retrieval", features.advanced_rag.dense_retrieval],
    ["Sparse retrieval", "sparse_retrieval", features.advanced_rag.sparse_retrieval],
    ["Reranking", "reranking", features.advanced_rag.reranking],
  ] as const : [];
  const memoryOptions = features ? [
    ["Short-term memory", "short_term", features.memory.short_term],
    ["Summarization", "summarization", features.memory.summarization],
    ["Long-term memory", "long_term", features.memory.long_term],
  ] as const : [];
  return (
    <>
      <FeatureSettingsPanel disabled={saving} enabled={features?.advanced_rag.enabled ?? false} hidden={section !== "rag"} onEnabledChange={(enabled) => features && onSave({ ...features, advanced_rag: { ...features.advanced_rag, enabled } })} options={advancedOptions} onOptionChange={(key, value) => features && onSave({ ...features, advanced_rag: { ...features.advanced_rag, [key]: value } })} title="Advanced RAG" />
      <FeatureSettingsPanel disabled={saving} enabled={features?.memory.enabled ?? false} hidden={section !== "memory"} onEnabledChange={(enabled) => features && onSave({ ...features, memory: { ...features.memory, enabled } })} options={memoryOptions} onOptionChange={(key, value) => features && onSave({ ...features, memory: { ...features.memory, [key]: value } })} title="Memory" />
      {status && (section === "rag" || section === "memory") && <output className="settings-status">{status}</output>}
    </>
  );
}

function optimisticMessages(input: string, attachments: Message["attachments"] = []) {
  const timestamp = new Date().toISOString();
  const userId = `optimistic-user-${crypto.randomUUID()}`;
  const assistantId = `streaming-assistant-${crypto.randomUUID()}`;
  return {
    userId,
    assistantId,
    messages: [
      { id: userId, role: "user", content: input, created_at: timestamp, attachments },
      { id: assistantId, role: "assistant", content: "", created_at: timestamp, attachments: [], pending: true },
    ] satisfies DraftMessage[],
  };
}

function evaluationGateStatus(run: EvaluationRun): string {
  if (!run.summary) return "Awaiting results";
  return run.summary.gate.passed ? "Gate passed" : "Gate failed";
}


export function ChatWorkspace() {
  const [models, setModels] = useState<ModelCatalog | null>(null);
  const [selectedModel, setSelectedModel] = useState("");
  const [modelChanging, setModelChanging] = useState(false);
  const [agent, setAgent] = useState<AgentSummary | null>(null);
  const [config, setConfig] = useState<Record<string, unknown>>({});
  const [guardrails, setGuardrails] = useState<GuardrailSettings | null>(null);
  const [runtimeFeatures, setRuntimeFeatures] = useState<RuntimeFeatureSettings | null>(null);
  const [guardrailMessage, setGuardrailMessage] = useState("");
  const [keywordDraft, setKeywordDraft] = useState("");
  const [profanityDraft, setProfanityDraft] = useState("");
  const [secretDraft, setSecretDraft] = useState("");
  const [pendingSecrets, setPendingSecrets] = useState<string[]>([]);
  const [removedSecretIds, setRemovedSecretIds] = useState<string[]>([]);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [settingsStatus, setSettingsStatus] = useState<string | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [active, setActive] = useState<Conversation | null>(null);
  const [messages, setMessages] = useState<DraftMessage[]>([]);
  const [prompt, setPrompt] = useState("");
  const [contextEstimate, setContextEstimate] = useState<ContextEstimate | null>(null);
  const [loading, setLoading] = useState(true);
  const [streaming, setStreaming] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("tools");
  const [navigationOpen, setNavigationOpen] = useState(false);
  const isMobile = useIsMobile();
  const mobileNavigationOpen = isMobile && navigationOpen;
  const [runId, setRunId] = useState<string | null>(null);
  const [usage, setUsage] = useState<{ input: number; output: number } | null>(null);
  const [evaluationTab, setEvaluationTab] = useState<"run" | "history">("run");
  const [evaluationConfigurations, setEvaluationConfigurations] = useState<EvaluationConfiguration[]>([]);
  const [evaluationDatasets, setEvaluationDatasets] = useState<EvaluationDataset[]>([]);
  const [evaluationRuns, setEvaluationRuns] = useState<EvaluationRun[]>([]);
  const [evaluationLoading, setEvaluationLoading] = useState(false);
  const [evaluationSubmitting, setEvaluationSubmitting] = useState(false);
  const [evaluationError, setEvaluationError] = useState<string | null>(null);
  const [datasetName, setDatasetName] = useState("");
  const [datasetVersion, setDatasetVersion] = useState("1");
  const [datasetInput, setDatasetInput] = useState("");
  const [datasetExpected, setDatasetExpected] = useState("");
  const [selectedDataset, setSelectedDataset] = useState("");
  const [baselineConfiguration, setBaselineConfiguration] = useState("");
  const [candidateConfiguration, setCandidateConfiguration] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadStatus, setUploadStatus] = useState<string | null>(null);
  const [attachment, setAttachment] = useState<SelectedAttachment | null>(null);
  const [previewAttachment, setPreviewAttachment] = useState<PreviewAttachment | null>(null);
  const { theme, toggleTheme, setTheme, skin, setSkin } = useTheme();
  const endRef = useRef<HTMLDivElement>(null);
  const promptRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const whyInputLocked = useRef(false);
  const speech = useSpeechInput((transcript) => {
    if (whyInputLocked.current) return;
    setPrompt((current) => appendTranscript(current, transcript));
    promptRef.current?.focus();
  });
  const why = useWhyAction(active?.id ?? null, messages.some((m) => m.role === "assistant" && !m.pending && !m.failed), speech.cancel, whyInputLocked);
  const { allowedExtensions, maxSourceBytes } = ragUploadLimits(config);


  useEffect(() => () => {
    if (attachment?.previewUrl) URL.revokeObjectURL(attachment.previewUrl);
  }, [attachment]);



  const refreshConversations = useCallback(async () => {
    const items = await apiJson<Conversation[]>("conversations?limit=50");
    setConversations(items);
    return items;
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void apiJson<ModelCatalog>("models", { signal: controller.signal })
      .then(validateModelCatalog).then(setModels).catch(() => { if (!controller.signal.aborted) setModels(null); });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    void (async () => {
      try {
        const [agentValue, configValue, guardrailValue, runtimeFeatureValue, items] = await Promise.all([
          apiJson<AgentSummary>("agent"),
          apiJson<Record<string, unknown>>("config"),
          apiJson<GuardrailSettings>("guardrails"),
          apiJson<RuntimeFeatureSettings>("runtime-settings"),
          refreshConversations(),
        ]);
        setAgent(agentValue);
        setSelectedModel(agentValue.model_alias ?? "");
        setConfig(configValue);
        setGuardrails(guardrailValue);
        setRuntimeFeatures(runtimeFeatureValue);
        setGuardrailMessage(guardrailValue.block_message);
        if (items[0]) {
          const first = await apiJson<Conversation>(`conversations/${items[0].id}`);
          setActive(first);
          setMessages(first.messages);
        }
      } catch (error_) {
        setError(error_ instanceof Error ? error_.message : "Could not connect to Talos.");
      } finally {
        setLoading(false);
      }
    })();
  }, [refreshConversations]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: streaming ? "smooth" : "auto" });
  }, [messages, streaming, why.reports]);

  useEffect(() => {
    if (loading || streaming || why.active || why.busy) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void apiJson<ContextEstimate>("context/estimate", {
        method: "POST",
        body: JSON.stringify({ draft: prompt, conversation_id: active?.id ?? null }),
        signal: controller.signal,
      }).then(setContextEstimate).catch(() => {
        if (!controller.signal.aborted) setContextEstimate(null);
      });
    }, 350);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [active?.id, loading, messages.length, prompt, streaming, why.active, why.busy]);

  const refreshEvaluations = useCallback(async () => {
    const [configurationValue, datasetValue, runValue] = await Promise.all([
      apiJson<{ configurations: EvaluationConfiguration[] }>("evaluation-configurations"),
      apiJson<{ datasets: EvaluationDataset[] }>("evaluation-datasets?limit=50"),
      apiJson<{ runs: EvaluationRun[] }>("evaluation-runs?limit=50"),
    ]);
    setEvaluationConfigurations(configurationValue.configurations);
    setEvaluationDatasets(datasetValue.datasets);
    setEvaluationRuns(runValue.runs);
    setSelectedDataset((current) => current || datasetValue.datasets[0]?.id || "");
    setBaselineConfiguration(
      (current) => current || configurationValue.configurations[1]?.hash || "",
    );
    setCandidateConfiguration(
      (current) => current || configurationValue.configurations[0]?.hash || "",
    );
  }, []);

  useEffect(() => {
    if (!settingsOpen || settingsSection !== "evaluations") return;
    let activeRequest = true;
    let timer: number | undefined;
    const refresh = async () => {
      try {
        await refreshEvaluations();
        if (activeRequest) setEvaluationError(null);
      } catch (error_) {
        if (activeRequest) {
          setEvaluationError(
            error_ instanceof Error ? error_.message : "Could not load evaluation data.",
          );
        }
      } finally {
        if (activeRequest) setEvaluationLoading(false);
      }
      if (activeRequest) timer = window.setTimeout(refresh, 3000);
    };
    void refresh();
    return () => {
      activeRequest = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [refreshEvaluations, settingsOpen, settingsSection]);

  async function createEvaluationDataset(event: SyntheticEvent<HTMLFormElement, SubmitEvent>) {
    event.preventDefault();
    setEvaluationSubmitting(true);
    setEvaluationError(null);
    try {
      const dataset = await apiJson<EvaluationDataset>("evaluation-datasets", {
        method: "POST",
        body: JSON.stringify({
          name: datasetName,
          version: Number(datasetVersion),
          cases: [{ input: datasetInput, expected_output: datasetExpected }],
        }),
      });
      setDatasetName("");
      setDatasetInput("");
      setDatasetExpected("");
      setSelectedDataset(dataset.id);
      await refreshEvaluations();
    } catch (error_) {
      setEvaluationError(
        error_ instanceof Error ? error_.message : "Could not create the evaluation dataset.",
      );
    } finally {
      setEvaluationSubmitting(false);
    }
  }

  async function createEvaluationRun(event: SyntheticEvent<HTMLFormElement, SubmitEvent>) {
    event.preventDefault();
    setEvaluationSubmitting(true);
    setEvaluationError(null);
    try {
      await apiJson<EvaluationRun>("evaluation-runs", {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({
          dataset_id: selectedDataset,
          baseline_configuration_hash: baselineConfiguration,
          candidate_configuration_hash: candidateConfiguration,
          seed: 0,
          gates: {
            minimum_candidate_pass_rate: 0,
            maximum_pass_rate_drop: 0,
            maximum_candidate_failure_rate: 0,
            maximum_candidate_p95_latency_ms: 30000,
          },
        }),
      });
      await refreshEvaluations();
    } catch (error_) {
      setEvaluationError(
        error_ instanceof Error ? error_.message : "Could not start the evaluation run.",
      );
    } finally {
      setEvaluationSubmitting(false);
    }
  }

  async function cancelEvaluationRun(runIdToCancel: string) {
    setEvaluationSubmitting(true);
    setEvaluationError(null);
    try {
      await apiJson<EvaluationRun>(`evaluation-runs/${runIdToCancel}/cancel`, {
        method: "POST",
      });
      await refreshEvaluations();
    } catch (error_) {
      setEvaluationError(
        error_ instanceof Error ? error_.message : "Could not cancel the evaluation run.",
      );
    } finally {
      setEvaluationSubmitting(false);
    }
  }

  async function selectConversation(conversation: Conversation) {
    if (streaming || why.busy) return;
    try {
      why.remove();
      setError(null);
      setActive(conversation);
      setMessages(conversation.messages);
      if (!why.active) setAttachment(null);
      setUploadStatus(null);
      setNavigationOpen(false);
      const current = await apiJson<Conversation>(`conversations/${conversation.id}`);
      setActive(current);
      setMessages(current.messages);
    } catch (error_) {
      setError(error_ instanceof Error ? error_.message : "Could not open this conversation.");
    }
  }

  async function handleNewConversation() {
    if (why.busy) return;
    try {
      await newConversation();
    } catch (error_) {
      setError(error_ instanceof Error ? error_.message : "Could not create a conversation.");
    }
  }

  async function changeModel(identifier: string) {
    if (streaming || modelChanging || uploading || why.active || why.busy || attachment) return;
    setModelChanging(true);
    speech.cancel();
    try {
      await newConversation(identifier);
      setUploadStatus("Model selected for a new chat. Your previous conversation is saved.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not select this model. Your current chat is unchanged.");
    } finally { setModelChanging(false); }
  }

  async function newConversation(modelProfile?: string): Promise<Conversation> {
    const requestedModel = modelProfile || active?.model_profile_id || selectedModel || models?.default_model_id;
    const created = await apiJson<Omit<Conversation, "messages">>("conversations", {
      method: "POST",
      body: JSON.stringify({ model_profile_id: requestedModel || null }),
    });
    const conversation: Conversation = { ...created, messages: [] };
    why.remove();
    setActive(conversation);
    setSelectedModel(conversation.model_profile_id || requestedModel || "");
    setMessages([]);
    if (!why.active) setAttachment(null);
    setUploadStatus(null);
    setUsage(null);
    setError(null);
    setNavigationOpen(false);
    setConversations((current) => [conversation, ...current]);
    promptRef.current?.focus();
    return conversation;
  }

  async function selectDocument(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file || uploading || why.active || why.busy) return;
    const dot = file.name.lastIndexOf(".");
    const extension = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
    if (!allowedExtensions.length || !maxSourceBytes) {
      setError("RAG upload limits are unavailable. Reload the workspace and try again.");
      return;
    }
    if (!allowedExtensions.includes(extension)) {
      setError(`Unsupported document type. Allowed: ${allowedExtensions.join(", ")}.`);
      return;
    }
    if (file.size > maxSourceBytes) {
      setError(`Document exceeds the configured ${maxSourceBytes.toLocaleString()} byte limit.`);
      return;
    }

    setError(null);
    setUploadStatus(null);
    const isPdf = extension === ".pdf";
    let previewText: string | null = null;
    try {
      previewText = isPdf ? null : (await file.text()).slice(0, 200_000);
    } catch {
      setError("The selected document could not be read.");
      return;
    }
    setAttachment({
      file,
      sourceId: null,
      name: file.name,
      extension,
      sizeBytes: file.size,
      previewText,
      previewUrl: isPdf ? URL.createObjectURL(file) : null,
      previewTruncated: !isPdf && file.size > new Blob([previewText ?? ""]).size,
      indexed: false,
    });
  }

  async function indexAttachment(file: File, conversationId: string): Promise<string> {
    setUploading(true);
    setUploadStatus(`Uploading ${file.name}...`);
    try {
      const created = await apiJson<KnowledgeJobCreated>("knowledge", {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({
          name: file.name,
          content: await knowledgeContent(file),
          conversation_id: conversationId,
        }),
      });
      for (let attempt = 0; attempt < 120; attempt += 1) {
        const job = await apiJson<KnowledgeJobStatus>(`knowledge/jobs/${created.job_id}`);
        if (job.state === "completed") {
          if (!job.source_id) throw new Error("Document indexing completed without a source reference.");
          setUploadStatus(`${file.name} is indexed and ready for cited answers.`);
          return job.source_id;
        }
        if (job.state === "failed" || job.state === "cancelled") {
          throw new Error(job.error_code ? `Document indexing failed (${job.error_code}).` : "Document indexing did not complete.");
        }
        setUploadStatus(job.state === "processing" ? `Indexing ${file.name}...` : `${file.name} is queued for indexing...`);
        await wait(750);
      }
      throw new Error("Document indexing is taking longer than expected. Check knowledge jobs later.");
    } catch (error_) {
      setUploadStatus(null);
      throw error_;
    } finally {
      setUploading(false);
    }
  }

  async function openMessageAttachment(sourceId: string) {
    if (!active) return;
    try {
      setError(null);
      const preview = await apiJson<KnowledgePreview>(
        `knowledge/${encodeURIComponent(sourceId)}/preview?conversation_id=${encodeURIComponent(active.id)}`,
      );
      const dot = preview.name.lastIndexOf(".");
      setPreviewAttachment({
        sourceId: preview.source_id,
        name: preview.name,
        extension: dot >= 0 ? preview.name.slice(dot).toLowerCase() : "",
        sizeBytes: null,
        previewText: preview.content,
        previewUrl: null,
        previewTruncated: preview.truncated,
      });
    } catch (error_) {
      setError(error_ instanceof Error ? error_.message : "The document preview could not be loaded.");
    }
  }
  function applyRuntimeEvent(event: RuntimeEvent, assistantId: string) {
    setRunId(event.run_id);
    if (event.type === "tool.completed" || event.type === "tool.failed") {
      const name = event.payload.name;
      if (typeof name === "string" && name) {
        const status = event.type === "tool.completed" ? "completed" as const : "failed" as const;
        setMessages((current) => current.map((message) => message.id === assistantId
          ? { ...message, run_id: event.run_id, tool_usages: [...(message.tool_usages ?? []).filter((tool) => tool.name !== name || tool.status !== status), { name, status }] } : message));
      }
    }
    if (event.type === "memory.compacting") {
      setMessages((current) => current.some((message) => message.id === `compaction-${event.run_id}`)
        ? current
        : [...current, { id: `compaction-${event.run_id}`, role: "notice", content: "Context automatically compacted", created_at: event.occurred_at, attachments: [], pending: true }]);
    } else if (event.type === "memory.summary_created") {
      setMessages((current) => current.map((message) => message.id === `compaction-${event.run_id}` ? { ...message, pending: false } : message));
    } else if (event.type === "response.delta") {
      const delta = typeof event.payload.text === "string" ? event.payload.text : "";
      setMessages((current) =>
        current.map((message) =>
          message.id === assistantId
            ? { ...message, content: message.content + delta, pending: true }
            : message,
        ),
      );
    } else if (event.type === "response.completed") {
      const eventUsage = event.payload.usage as
        | { input_tokens?: number; output_tokens?: number }
        | undefined;
      setUsage({
        input: Number(eventUsage?.input_tokens ?? 0),
        output: Number(eventUsage?.output_tokens ?? 0),
      });
      setMessages((current) =>
        current.map((message) =>
          message.id === assistantId ? { ...message, pending: false } : message,
        ),
      );
    } else if (event.type === "run.failed") {
      setError(
        typeof event.payload.message === "string"
          ? event.payload.message
          : "The Talos run failed. Try again.",
      );
    } else if (event.type === "run.cancelled") {
      setError("Generation stopped. Your conversation is still available.");
    }
  }

  async function sendMessage(event?: SyntheticEvent<HTMLFormElement, SubmitEvent>) {
    event?.preventDefault();
    if (why.active) { await why.send(); return; }
    if (why.busy) return;
    const input = prompt.trim();
    const attachmentToSend = attachment;
    if (!input || streaming || uploading || modelChanging) return;

    speech.cancel();
    setError(null);
    setPrompt("");
    setStreaming(true);
    setCancelling(false);
    let conversation = active;
    let attachmentSourceId = attachmentToSend?.sourceId ?? null;
    let optimisticUserId: string | null = null;
    let assistantId: string | null = null;
    try {
      conversation ??= await newConversation();
      if (attachmentToSend && !attachmentToSend.indexed) {
        attachmentSourceId = await indexAttachment(attachmentToSend.file, conversation.id);
        setAttachment((current) => current ? { ...current, indexed: true, sourceId: attachmentSourceId } : current);
      }
      const messageAttachments = attachmentToSend && attachmentSourceId
        ? [{ source_id: attachmentSourceId, name: attachmentToSend.name }]
        : [];
      const optimistic = optimisticMessages(input, messageAttachments);
      optimisticUserId = optimistic.userId;
      assistantId = optimistic.assistantId;
      setMessages((current) => [...current, ...optimistic.messages]);

      const response = await fetch("/api/talos/chat/stream", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify({ input, conversation_id: conversation.id, attachments: messageAttachments }),
      });
      const stream = await consumeChatStream(
        response,
        (runtimeEvent) => applyRuntimeEvent(runtimeEvent, optimistic.assistantId),
      );
      if (!stream.completed) {
        throw new Error(stream.failure ?? "The response stream ended before Talos completed the message.");
      }
      await refreshConversations();
      const current = await apiJson<Conversation>(`conversations/${conversation.id}`);
      setActive(current);
      setMessages(current.messages);
      if (attachmentToSend) setAttachment(null);
    } catch (error_) {
      const message = error_ instanceof Error ? error_.message : "The request could not be completed.";
      setError(message);
      setPrompt(input);
      setMessages((current) =>
        current
          .filter((item) => item.id !== assistantId)
          .map((item) =>
            item.id === optimisticUserId
              ? { ...item, failed: true, pending: false }
              : item,
          ),
      );
    } finally {
      setStreaming(false);
      setCancelling(false);
      setRunId(null);
      promptRef.current?.focus();
    }
  }

  async function cancelRun() {
    if (!runId || cancelling) return;
    setCancelling(true);
    try {
      await apiJson(`runs/${runId}/cancel`, { method: "POST" });
    } catch (error_) {
      setError(error_ instanceof Error ? error_.message : "Cancellation failed.");
      setCancelling(false);
    }
  }

  function handlePromptKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void sendMessage();
    }
  }

  function addGuardrailItem(kind: "keyword" | "profanity" | "secret") {
    let draft = secretDraft;
    if (kind === "keyword") draft = keywordDraft;
    if (kind === "profanity") draft = profanityDraft;
    const value = draft.trim();
    if (!value || !guardrails) return;
    if (kind === "keyword") {
      if (!guardrails.custom_keywords.some((item) => item.toLowerCase() === value.toLowerCase())) {
        setGuardrails({ ...guardrails, custom_keywords: [...guardrails.custom_keywords, value] });
      }
      setKeywordDraft("");
    } else if (kind === "profanity") {
      if (!guardrails.custom_profanity.some((item) => item.toLowerCase() === value.toLowerCase())) {
        setGuardrails({ ...guardrails, custom_profanity: [...guardrails.custom_profanity, value] });
      }
      setProfanityDraft("");
    } else {
      setPendingSecrets((current) => [...current, value]);
      setSecretDraft("");
    }
    setSettingsStatus(null);
  }

  async function saveGuardrails(event: SyntheticEvent<HTMLFormElement, SubmitEvent>) {
    event.preventDefault();
    if (!guardrails || settingsSaving) return;
    setSettingsSaving(true);
    setSettingsStatus(null);
    try {
      const updated = await apiJson<GuardrailSettings>("guardrails", {
        method: "PATCH",
        body: JSON.stringify({
          block_message: guardrailMessage,
          custom_keywords: guardrails.custom_keywords,
          custom_profanity: guardrails.custom_profanity,
          add_secrets: pendingSecrets,
          remove_secret_ids: removedSecretIds,
        }),
      });
      setGuardrails(updated);
      setGuardrailMessage(updated.block_message);
      setPendingSecrets([]);
      setRemovedSecretIds([]);
      setSettingsStatus("Guardrail settings saved.");
    } catch (error_) {
      setSettingsStatus(
        error_ instanceof Error ? error_.message : "Guardrail settings could not be saved.",
      );
    } finally {
      setSettingsSaving(false);
    }
  }

  async function saveRuntimeFeatures(next: RuntimeFeatureSettings) {
    if (settingsSaving) return;
    const previous = runtimeFeatures;
    setRuntimeFeatures(next);
    setSettingsSaving(true);
    setSettingsStatus(null);
    try {
      const updated = await apiJson<RuntimeFeatureSettings>("runtime-settings", {
        method: "PATCH",
        body: JSON.stringify({ advanced_rag: next.advanced_rag, memory: next.memory }),
      });
      setRuntimeFeatures(updated);
      setAgent((current) => current ? { ...current, configuration_version: updated.configuration_version } : current);
      setConfig(await apiJson<Record<string, unknown>>("config"));
      setSettingsStatus("Runtime settings saved. New conversations use this configuration.");
    } catch (error_) {
      setRuntimeFeatures(previous);
      setSettingsStatus(error_ instanceof Error ? error_.message : "Runtime settings could not be saved.");
    } finally {
      setSettingsSaving(false);
    }
  }
  function focusSettingsPanel() {
    window.requestAnimationFrame(() => {
      const closeButton = document.getElementById("settings-close");
      closeButton?.focus({ preventScroll: true });
      closeButton?.closest(".settings-panel")?.scrollTo({ top: 0, behavior: "instant" });
    });
  }

  function showSettings(section: SettingsSection, revealNavigation = false) {
    setSettingsSection(section);
    setSettingsOpen(true);
    setNavigationOpen(revealNavigation);
    if (!revealNavigation) focusSettingsPanel();
  }

  function selectSettingsSection(section: SettingsSection) {
    setSettingsSection(section);
    if (section === "evaluations") setEvaluationLoading(true);
    setNavigationOpen(false);
    focusSettingsPanel();
  }

  function closeSettings() {
    setSettingsOpen(false);
    setNavigationOpen(false);
    window.requestAnimationFrame(() => promptRef.current?.focus());
  }

  function toggleSettings() {
    if (settingsOpen) {
      closeSettings();
    } else {
      showSettings("tools", true);
    }
  }
  return (
    <div className="workspace">
      {mobileNavigationOpen && <button
        aria-label="Close conversation navigation"
        className="mobile-scrim is-visible"
        onClick={() => setNavigationOpen(false)}
        type="button"
      />}
      <WorkspaceSidebar
        activeId={active?.id}
        conversations={conversations}
        loading={loading}
        modal={mobileNavigationOpen}
        navigationOpen={navigationOpen}
        onConversationSelect={selectConversation}
        onNavigationClose={() => setNavigationOpen(false)}
        onNewConversation={handleNewConversation}
        onSettingsSectionSelect={selectSettingsSection}
        onSettingsToggle={toggleSettings}
        settingsOpen={settingsOpen}
        settingsSection={settingsSection}
        streaming={streaming}
      />

      <main className="main-panel" hidden={settingsOpen} inert={mobileNavigationOpen} id={settingsOpen ? undefined : "conversation"}>
        <WorkspaceTopbar active={active} agent={agent} onNavigationOpen={() => setNavigationOpen(true)} onSettingsOpen={() => showSettings("tools", true)} onThemeToggle={toggleTheme} theme={theme} />

        <section aria-busy={streaming} className="conversation">
          <p aria-live="polite" className="sr-only">{streaming ? "Talos is responding." : ""}</p>
          <ConversationContent whyReports={why.reports} active={active} agent={agent} endRef={endRef} loading={loading} messages={messages} onAttachmentOpen={(sourceId) => void openMessageAttachment(sourceId)} onSuggestion={setPrompt} streaming={streaming} usage={usage} />
        </section>

        <Composer models={models} selectedModel={active?.model_profile_id || selectedModel || models?.default_model_id || ""} modelChanging={modelChanging} onModelChange={(id) => void changeModel(id)} why={why} allowedExtensions={allowedExtensions} attachment={attachment} cancelling={cancelling} contextEstimate={contextEstimate} error={error} fileRef={fileRef} onAttachmentOpen={() => attachment && setPreviewAttachment(attachment)} onAttachmentRemove={() => setAttachment(null)} onCancel={() => void cancelRun()} onDismissError={() => setError(null)} onDocumentSelect={(event) => void selectDocument(event)} onPromptChange={(value) => { if (!whyInputLocked.current) setPrompt(value); }} onPromptKeyDown={handlePromptKeyDown} onSubmit={(event) => void sendMessage(event)} prompt={prompt} promptRef={promptRef} runId={runId} speech={speech} streaming={streaming} uploading={uploading} uploadStatus={uploadStatus} />
      </main>

      <main
        id={settingsOpen ? "conversation" : undefined}
        inert={mobileNavigationOpen}
        aria-label="Settings"
        className={`settings-panel ${settingsOpen ? "is-open" : ""}`}
        hidden={!settingsOpen}
      >
        <div className="settings-heading">
          <button
            aria-label="Close settings"
            className="icon-button"
            id="settings-close"
            onClick={closeSettings}
            type="button"
          >
            <CloseIcon />
          </button>
        </div>
        <section
          className="settings-view runtime-settings-view"
          hidden={settingsSection !== "runtime"}
        >
          <dl className="settings-list">
            <div><dt>Agent</dt><dd>{agent?.name ?? "-"}</dd></div>
            <div><dt>Provider</dt><dd>{agent?.provider ?? "-"}</dd></div>
            <div><dt>Model</dt><dd>{agent?.model ?? "-"}</dd></div>
            <div><dt>Pipeline</dt><dd>{agent?.pipeline ?? "-"}</dd></div>
            <div><dt>Current version</dt><dd>v{agent?.configuration_version ?? "-"}</dd></div>
          </dl>
          {settingsOpen && settingsSection === "runtime" && <ModelSettings catalog={models} onChange={setModels} defaultContext={Number((config.model as Record<string, unknown> | undefined)?.context_window_tokens) || 8192} defaultOutput={Number((config.model as Record<string, unknown> | undefined)?.max_output_tokens) || 1024} />}
          <details className="config-details">
            <summary>Safe resolved configuration</summary>
            <pre>{JSON.stringify(config, null, 2)}</pre>
          </details>
          <div className="snapshot-card">
            <strong>Snapshot behavior</strong>
          </div>
        </section>
        <RuntimeFeaturePanels features={runtimeFeatures} onSave={(next) => void saveRuntimeFeatures(next)} saving={settingsSaving} section={settingsSection} status={settingsStatus} />
        {settingsOpen && settingsSection === "debug" && <DebugSettings />}
        {settingsOpen && (settingsSection === "tools" || settingsSection === "skills") && <ExtensionSettings key={settingsSection} section={settingsSection} />}
        <AppearanceSettings hidden={settingsSection !== "appearance"} theme={theme} skin={skin} onThemeChange={setTheme} onSkinChange={setSkin} />
        <section className="settings-view monitoring-view" hidden={settingsSection !== "monitoring"}>
          {settingsOpen && settingsSection === "monitoring" && <div className="monitoring-dashboard-current"><MonitoringDashboard refreshKey={runId} /></div>}
        </section>
        {settingsOpen && settingsSection === "traces" && <TraceInspector />}
        <section className="settings-view evaluation-view" hidden={settingsSection !== "evaluations"}>
          <div aria-label="Evaluation views" className="evaluation-tabs" role="tablist">
            <button aria-selected={evaluationTab === "run"} onClick={() => setEvaluationTab("run")} role="tab" type="button">Run evaluation</button>
            <button aria-selected={evaluationTab === "history"} onClick={() => setEvaluationTab("history")} role="tab" type="button">History</button>
          </div>
          {evaluationLoading && <output className="monitoring-empty">Loading evaluation data...</output>}
          {evaluationError && <p className="settings-status" role="alert">{evaluationError}</p>}

          <div className="evaluation-actions" hidden={evaluationTab !== "run"}>
            <form className="evaluation-form" onSubmit={(event) => void createEvaluationDataset(event)}>
              <div><span>Step 1</span><h4>Create a versioned dataset</h4></div>
              <label htmlFor="evaluation-dataset-name">Dataset name</label>
              <input id="evaluation-dataset-name" maxLength={200} onChange={(event) => setDatasetName(event.target.value)} required value={datasetName} />
              <label htmlFor="evaluation-dataset-version">Version</label>
              <input id="evaluation-dataset-version" min="1" onChange={(event) => setDatasetVersion(event.target.value)} required type="number" value={datasetVersion} />
              <label htmlFor="evaluation-input">Case input</label>
              <textarea id="evaluation-input" maxLength={4000} onChange={(event) => setDatasetInput(event.target.value)} required rows={3} value={datasetInput} />
              <label htmlFor="evaluation-expected">Expected answer</label>
              <textarea id="evaluation-expected" maxLength={4000} onChange={(event) => setDatasetExpected(event.target.value)} required rows={3} value={datasetExpected} />
              <button className="save-settings-button" disabled={evaluationSubmitting} type="submit">
                {evaluationSubmitting ? "SavingÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¦" : "Save dataset"}
              </button>
            </form>

            <form className="evaluation-form" onSubmit={(event) => void createEvaluationRun(event)}>
              <div><span>Step 2</span><h4>Run a paired comparison</h4></div>
              <label htmlFor="evaluation-dataset">Dataset</label>
              <select id="evaluation-dataset" onChange={(event) => setSelectedDataset(event.target.value)} required value={selectedDataset}>
                <option value="">Select a dataset</option>
                {evaluationDatasets.map((dataset) => <option key={dataset.id} value={dataset.id}>{dataset.name} v{dataset.version} ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â· {dataset.case_count} cases</option>)}
              </select>
              <label htmlFor="evaluation-baseline">Baseline configuration</label>
              <select id="evaluation-baseline" onChange={(event) => setBaselineConfiguration(event.target.value)} required value={baselineConfiguration}>
                <option value="">Select baseline</option>
                {evaluationConfigurations.map((item) => <option key={item.hash} value={item.hash}>v{item.version} ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â· {item.model} ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â· {item.hash.slice(0, 8)}</option>)}
              </select>
              <label htmlFor="evaluation-candidate">Candidate configuration</label>
              <select id="evaluation-candidate" onChange={(event) => setCandidateConfiguration(event.target.value)} required value={candidateConfiguration}>
                <option value="">Select candidate</option>
                {evaluationConfigurations.map((item) => <option key={item.hash} value={item.hash}>v{item.version} ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â· {item.model} ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â· {item.hash.slice(0, 8)}</option>)}
              </select>
              <button className="save-settings-button" disabled={evaluationSubmitting || evaluationConfigurations.length < 2 || baselineConfiguration === candidateConfiguration} type="submit">
                {evaluationSubmitting ? "StartingÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¦" : "Run comparison"}
              </button>
            </form>
          </div>

          <section aria-labelledby="evaluation-history-title" className="evaluation-history evaluation-history-legacy" hidden>
            <div className="monitoring-card-heading">
              <div><span>Same dataset, paired outcomes</span><h3 id="evaluation-history-title">Evaluation history</h3></div>
              <strong>{evaluationRuns.length} runs</strong>
            </div>
            {evaluationRuns.length === 0 && !evaluationLoading ? <p className="monitoring-empty">Create a dataset and run the first comparison.</p> : (
              <div className="evaluation-run-list">
                {evaluationRuns.map((run) => (
                  <details className="evaluation-run" key={run.id}>
                    <summary>
                      <span><strong>{run.state}</strong><small>{run.id.slice(0, 8)} ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â· {run.result_count} results</small></span>
                      <span className={run.summary?.gate.passed ? "gate-pass" : "gate-neutral"}>{evaluationGateStatus(run)}</span>
                    </summary>
                    <dl className="trace-facts">
                      <div><dt>Evaluator</dt><dd>{run.evaluator_version}</dd></div>
                      <div><dt>Seed</dt><dd>{run.seed}</dd></div>
                      <div><dt>Changed fields</dt><dd>{run.changed_fields.length}</dd></div>
                      <div><dt>Attempts</dt><dd>{run.attempt_count}</dd></div>
                    </dl>
                    {run.summary && (
                      <div className="evaluation-comparison">
                        {(["baseline", "candidate"] as const).map((variant) => {
                          const result = run.summary?.[variant];
                          if (!result) return null;
                          return <article key={variant}>
                            <div><strong>{variant}</strong><span>{Math.round(result.pass_rate * 100)}% passed</span></div>
                            <div aria-label={`${variant} pass rate ${Math.round(result.pass_rate * 100)} percent`} className="evaluation-meter"><i style={{ width: `${Math.round(result.pass_rate * 100)}%` }} /></div>
                            <small>P95 {result.p95_latency_ms.toLocaleString()} ms ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â· {(result.input_tokens + result.output_tokens).toLocaleString()} tokens</small>
                          </article>;
                        })}
                        <p className="evaluation-delta">Candidate pass-rate change: {(run.summary.pass_rate_delta * 100).toFixed(1)} percentage points.</p>
                        {!run.summary.gate.passed && <p className="trace-error">Gate reasons: {run.summary.gate.reasons.join(", ").replaceAll("_", " ")}</p>}
                      </div>
                    )}
                    {(run.state === "pending" || run.state === "processing") && (
                      <button className="evaluation-cancel" disabled={evaluationSubmitting} onClick={() => void cancelEvaluationRun(run.id)} type="button">Cancel run</button>
                    )}
                    {run.error_code && <p className="trace-error">{run.error_code.replaceAll("_", " ")}</p>}
                    <details className="config-details"><summary>Controlled configuration differences</summary><ul>{run.changed_fields.map((field) => <li key={field}>{field}</li>)}</ul></details>
                  </details>
                ))}
              </div>
            )}
          </section>
          {evaluationTab === "history" && <EvaluationHistory runs={evaluationRuns} />}
        </section>
        <form className="settings-view guardrail-settings" hidden={settingsSection !== "guardrails"} onSubmit={(event) => void saveGuardrails(event)}>
          <label htmlFor="guardrail-message">Blocker message</label>
          <textarea
            id="guardrail-message"
            maxLength={800}
            onChange={(event) => {
              setGuardrailMessage(event.target.value);
              setSettingsStatus(null);
            }}
            required
            rows={3}
            value={guardrailMessage}
          />

          <GuardrailAdder
            draft={keywordDraft}
            id="guardrail-keyword"
            items={guardrails?.custom_keywords ?? []}
            label="Custom blocked keywords"
            onAdd={() => addGuardrailItem("keyword")}
            onChange={setKeywordDraft}
            onRemove={(value) =>
              guardrails &&
              setGuardrails({
                ...guardrails,
                custom_keywords: guardrails.custom_keywords.filter((item) => item !== value),
              })
            }
            placeholder="e.g. Why"
          />

          <section className="guardrail-group" aria-labelledby="secrets-heading">
            <div className="guardrail-group-title">
              <div>
                <h4 id="secrets-heading">Secrets masking</h4>
                <p>Passwords, API keys, tokens, private keys, and PII are always blocked.</p>
              </div>
            </div>
            <div className="adder-row">
              <label className="sr-only" htmlFor="guardrail-secret">Add a secret value</label>
              <input
                autoComplete="off"
                id="guardrail-secret"
                onChange={(event) => setSecretDraft(event.target.value)}
                placeholder="Add another secret value"
                type="password"
                value={secretDraft}
              />
              <button
                aria-label="Add custom secret"
                className="add-rule-button"
                disabled={!secretDraft.trim()}
                onClick={() => addGuardrailItem("secret")}
                type="button"
              >
                <PlusIcon />
              </button>
            </div>
            <div className="rule-chips">
              {guardrails?.custom_secrets.map((item) => (
                <span className="rule-chip" key={item.id}>
                  {item.fingerprint}
                  <button
                    aria-label={`Remove custom secret ${item.fingerprint}`}
                    onClick={() => {
                      setRemovedSecretIds((current) => [...current, item.id]);
                      setGuardrails({
                        ...guardrails,
                        custom_secrets: guardrails.custom_secrets.filter(
                          (secret) => secret.id !== item.id,
                        ),
                      });
                    }}
                    type="button"
                  >
                    <CloseIcon />
                  </button>
                </span>
              ))}
              {pendingSecrets.map((value) => (
                <span className="rule-chip" key={value}>Pending secret</span>
              ))}
            </div>
          </section>

          <GuardrailAdder
            draft={profanityDraft}
            id="guardrail-profanity"
            items={guardrails?.custom_profanity ?? []}
            label="Additional profanity"
            onAdd={() => addGuardrailItem("profanity")}
            onChange={setProfanityDraft}
            onRemove={(value) =>
              guardrails &&
              setGuardrails({
                ...guardrails,
                custom_profanity: guardrails.custom_profanity.filter((item) => item !== value),
              })
            }
            placeholder="Add another blocked word"
          />

          <section className="immutable-rules" aria-labelledby="immutable-heading">
            <h4 id="immutable-heading">Non-negotiable protections</h4>
            {(guardrails?.immutable ?? [])
              .filter((item) => !["secrets", "pii", "profanity"].includes(item.id))
              .map((item) => (
                <div className="immutable-rule" key={item.id}>
                  <strong>{item.label}</strong>
                  <span>{item.detail}</span>
                </div>
              ))}
          </section>

          <button className="save-settings-button" disabled={!guardrails || settingsSaving} type="submit">
            {settingsSaving ? "Saving..." : "Save guardrails"}
          </button>
          <p aria-live="polite" className="settings-status">{settingsStatus}</p>
        </form>
      </main>
      {previewAttachment && (
        <AttachmentPreview attachment={previewAttachment} onClose={() => setPreviewAttachment(null)} />
      )}
    </div>
  );
}
