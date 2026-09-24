/**
 * Named wire types for the DeepSeek Harness SDK runtime protocol: the three
 * request/result pairs and the four server-to-client notification payloads
 * exchanged over the newline-delimited JSON-RPC stdio transport. The server
 * plugin (`@deepseek-ai/dsh-sdk-jsonrpc-server`) and SDK clients share these shapes;
 * `serverInfo.name` stays the wire-stable `deepseek-harness-sdk-runtime`.
 *
 * @module @deepseek-ai/dsh-sdk-protocol/types
 */

import type { ContentBlock, ReasoningEffortId } from '@deepseek-ai/dsh-llm'
import type { SessionEvent } from '@deepseek-ai/dsh-session'
import type { SubagentStopReason } from '@deepseek-ai/dsh-subagent'

/** Parameters for the process-wide SDK handshake. */
export interface InitializeParams {
  /** Working directory recorded on every SDK-created session's header. */
  cwd: string
  /** Provider route every SDK-created agent runs on. */
  provider: string
  /** Model name every SDK-created agent runs on (the server may mount a fallback adapter; see `HarnessSdkJsonRpcServer.initialize`). */
  model: string
  /** Optional adapter-owned reasoning effort for the selected provider/model route. */
  reasoningEffort?: ReasoningEffortId
  /** Optional positive output-token cap inherited by SDK-created agents and their in-process descendants. */
  maxTokens?: number
}

/** Wire-stable server identity returned by initialization. */
export interface InitializeResult {
  /** Wire-stable server identity (`deepseek-harness-sdk-runtime`) and version. */
  serverInfo: { name: string; version: string }
}

/** One user turn on one SDK session. */
export interface SessionPromptParams {
  /** The SDK-side session id; an unknown id lazily creates the agent+session pair. */
  sessionId: string
  /** The prompt content blocks, sent verbatim as the user message. */
  contentBlocks: SdkPromptContentBlock[]
}

/** Inline raster input admitted into the runtime's durable attachment store. */
export interface SdkEncodedImageBlock {
  type: 'image'
  /** Canonical base64-encoded raster bytes. */
  data: string
  /** Declared raster MIME type, verified during admission. */
  mimeType: 'image/png' | 'image/jpeg' | 'image/webp' | 'image/gif'
}

/** SDK prompt input: ordinary durable blocks plus inline images awaiting admission. */
export type SdkPromptContentBlock = ContentBlock | SdkEncodedImageBlock

/** Durable enqueue receipt for one prompt. */
export interface SessionPromptResult {
  /** Identity of the queued user message. */
  messageId: string
}

/** Deployment-mapped SDK outcome: `ok` for an accepted result, `error` otherwise. */
export type SdkRunStatus = 'ok' | 'error'

/** `session.event` payload: one session-log event, streamed as it is recorded. */
export interface SessionEventNotification {
  /** Session the event belongs to (every session in the runtime, not only SDK-created ones). */
  sessionId: string
  /** The full session-log event envelope. */
  event: SessionEvent
}

/** Whole-agent lifecycle state for one session. */
export interface SessionStatusNotification {
  /** Session whose live agent changed status. */
  sessionId: string
  /** The whole-agent state after the transition. */
  status: 'idle' | 'running'
}

/** `subagent.started` payload: an in-runtime child session was created. */
export interface SubagentStartedNotification {
  /** The delegating session. */
  parentSessionId: string
  /** The new child session. */
  childSessionId: string
}

/** `subagent.finished` payload: an in-process subagent run ended (remote runs are not reported). */
export interface SubagentFinishedNotification {
  /** Subagent provider name that ran the child. */
  provider: string
  /** The child agent's id (equals {@link childSessionId} for local runs). */
  agentId: string
  /** The delegating session. */
  parentSessionId: string
  /** The child session. */
  childSessionId: string
  /** Deployment-mapped run outcome. */
  status: SdkRunStatus
  /** The provider-reported stop reason. */
  stopReason: SubagentStopReason
  /** The child's selected assistant output; absent when the child produced none. */
  lastAssistantMessage?: ContentBlock[]
}

/** Identity and metadata for one session in the runtime's session corpus. */
export interface SessionDescriptor {
  /** Durable session id (accepted by `session/prompt`, `session/resume`, and `session/history`). */
  sessionId: string
  /** Working directory recorded on the session header; absent when the session recorded none. */
  cwd?: string
  /** Session creation time in epoch milliseconds. */
  createdAt: number
  /** Latest folded title; absent when the log carries no `session/title` event. */
  title?: string
}

/** One `session/list` entry: a descriptor plus whether it is live and/or persisted. */
export interface SessionListEntry extends SessionDescriptor {
  /** Whether the id currently exists as a live agent in this runtime. */
  live: boolean
  /** Whether the active persistence backend currently lists the id. */
  persisted: boolean
  /** Whether the id sits in the registry-global archive set (hidden from grouping surfaces). */
  archived: boolean
}

/** Parameters for `session/list`. */
export interface SessionListParams {
  /** Optional exact working-directory filter over the session headers. */
  cwd?: string
  /** Optional positive cap on returned sessions; the corpus is newest-first. */
  limit?: number
}

/** `session/list` result: newest first. */
export interface SessionListResult {
  /** The matching sessions, newest first. */
  sessions: SessionListEntry[]
}

/** Parameters for `session/history`. */
export interface SessionHistoryParams {
  /** Stored session to read; an unknown id is an error. */
  sessionId: string
  /** Optional positive cap: when set, only the newest `limit` events are returned. */
  limit?: number
}

/** `session/history` result: one session's raw log, exactly as recorded. */
export interface SessionHistoryResult {
  /** Identity and metadata for the read session. */
  session: SessionDescriptor
  /** Raw log events in log order (the newest `limit` of them when a cap was given). */
  events: SessionEvent[]
  /** Whether events were dropped from the front of the log to satisfy `limit`. */
  truncated: boolean
}

/** Parameters for `session/resume`. */
export interface SessionResumeParams {
  /** Persisted session to make live again so later prompts continue its history. */
  sessionId: string
}

/** `session/resume` result. */
export interface SessionResumeResult {
  /** The resumed session id (echo of the request). */
  sessionId: string
  /** Whether this call resumed the session; `false` means it was already live here. */
  resumed: boolean
}

/** Parameters for `session/rename`. */
export interface SessionRenameParams {
  /** Live session to rename; a persisted-but-not-live session must be resumed first. */
  sessionId: string
  /** Raw user title; the title service normalizes and rejects non-visible input. */
  title: string
}

/** `session/rename` result. */
export interface SessionRenameResult {
  /** The renamed session id (echo of the request). */
  sessionId: string
  /** The accepted, normalized title as folded from the appended event. */
  title: string
}

/** Parameters for `session/archive` and `session/unarchive`. */
export interface SessionArchiveParams {
  /** Session to (un)archive; an id neither live nor persisted is an error. */
  sessionId: string
}

/** `session/archive` / `session/unarchive` result. */
export interface SessionArchiveResult {
  /** The affected session id (echo of the request). */
  sessionId: string
  /** The session's archive membership after the call. */
  archived: boolean
}

/** Server-to-client notifications by JSON-RPC method name. */
export interface HarnessSdkNotificationMap {
  'session.event': SessionEventNotification
  'session.status': SessionStatusNotification
  'subagent.started': SubagentStartedNotification
  'subagent.finished': SubagentFinishedNotification
}

/** Client-to-server request methods with their param and result shapes. */
export interface HarnessSdkRequestMap {
  'initialize': { params: InitializeParams; result: InitializeResult }
  'session/prompt': { params: SessionPromptParams; result: SessionPromptResult }
  'session/list': { params: SessionListParams; result: SessionListResult }
  'session/history': { params: SessionHistoryParams; result: SessionHistoryResult }
  'session/resume': { params: SessionResumeParams; result: SessionResumeResult }
  'session/rename': { params: SessionRenameParams; result: SessionRenameResult }
  'session/archive': { params: SessionArchiveParams; result: SessionArchiveResult }
  'session/unarchive': { params: SessionArchiveParams; result: SessionArchiveResult }
  'shutdown': { params: undefined; result: Record<string, never> }
}
