export interface HostInfo {
  protocol: string; workspace: string; instance_id: string; status: string;
  backend_version?: string; api_version?: number; capabilities?: string[];
}
export interface Selection { session_id: string; run_id: string }
export interface Workspace { handle: string; workspace: string; info: HostInfo; selection?: Selection }
export interface RecentWorkspace { id: string; workspace: string }
export interface Page<T> { items: T[]; next_cursor: string | null }
export interface Configuration { provider: string; model: string; endpoint: string; api_key_configured: boolean }
export interface SessionItem { id: string; created_at: string; provider_protocol: string }
export interface RunItem { run_id: string; session_id: string; parent_run_id: string; status: string; created_at: string; finished_at?: string }
export interface Run extends RunItem { last_sequence: number; final_answer: string; stop_reason: string; report?: { unknown_calls?: string[] } }
export interface Message { message_id: string; message_index: number; role: string; content: string; content_truncated: boolean; tool_calls?: { id: string; name: string }[]; tool_call_count?: number }
export interface Session extends SessionItem { config: Config; history: Message[]; next_message: number; history_count: number; history_revision?: string }
export interface Config { provider?: string; model?: string; approval?: string }
export interface ToolCall { run_id: string; call_id: string; call_ref?: string; name: string; phase: string; args?: unknown; result?: { status: string; error_code: string; data?: unknown }; details_truncated?: boolean; preview?: string }
export interface Approval { id: string; run_id: string; call_id: string; name: string; args: unknown; status: string; expires_at: string; scope: { cwd: string; path: string }; args_truncated: boolean }
export interface RunEvent { sequence: number; event_id: string; event: string; name?: string }
export interface Activity { subscription_id: string; handle: string; run_id: string; events: RunEvent[]; next_sequence: number; error?: string }
export interface Submission { session_id: string; prompt: string; client_request_id: string; config?: Config }
export interface HostMethods {
  'configuration.get': { params: { session_id?: string }; result: Configuration };
  'sessions.list': { params: { cursor: string; limit: number }; result: Page<SessionItem> };
  'sessions.create': { params: { config: Config }; result: Session };
  'sessions.get': { params: { session_id: string; after_message: number; limit: number }; result: Session };
  'runs.list': { params: { cursor: string; limit: number; session_id: string }; result: Page<RunItem> };
  'runs.get': { params: { run_id: string }; result: Run };
  'runs.start': { params: Submission; result: Run };
  'runs.cancel': { params: { run_id: string }; result: Run };
  'runs.tool_calls': { params: { run_id: string; cursor: string; limit: number }; result: Page<ToolCall> };
  'approvals.list': { params: Record<string, never>; result: Approval[] };
  'approvals.resolve': { params: { approval_id: string; approved: boolean }; result: Approval };
}
export interface DesktopAPI {
  chooseWorkspace(): Promise<Workspace | null>;
  recentWorkspaces(): Promise<RecentWorkspace[]>;
  openRecent(id: string): Promise<Workspace>;
  restoreWorkspace(): Promise<Workspace | null>;
  refreshWorkspace(handle: string): Promise<Workspace>;
  upgradeHost(handle: string): Promise<Workspace>;
  rememberSelection(handle: string, selection: Selection): Promise<void>;
  request<K extends keyof HostMethods>(handle: string, method: K, params: HostMethods[K]['params']): Promise<HostMethods[K]['result']>;
  watchRun(handle: string, subscriptionId: string, runId: string, after: number): Promise<void>;
  stopWatch(subscriptionId: string): Promise<void>;
  onActivity(callback: (activity: Activity) => void): () => void;
}
export function compatible(info: HostInfo) {
  return (info.api_version ?? 0) >= 4 && ['desktop_control', 'query_cursors', 'configuration_summary'].every(c => info.capabilities?.includes(c));
}
