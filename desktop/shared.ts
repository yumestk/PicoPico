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
export interface ToolCall { run_id: string; call_id: string; call_ref?: string; name: string; phase: string; args?: unknown; result?: { status: string; error_code: string; data?: { purpose?: string }; metadata?: { affected_paths?: string[] } }; details_truncated?: boolean; preview?: string }
export interface CheckSummary { status: string; counts: Record<string, number>; total: number; coverage_assessed: boolean; scope: string }
export interface Scope { captured_at?: string; complete: boolean; notes: string[]; file_count?: number; content_omissions?: Record<string, number> }
export interface Review { state: string; reason?: string; parent_run_id?: string; baseline: Scope | null; end: Scope | null; change_count: number; initial_change_count: number; checks: CheckSummary }
export interface Change { path: string; id?: string; kind?: string; reason?: string; diff_available?: boolean; index?: string; worktree?: string }
export interface Changes { files: Change[]; next_file: number; total: number }
export interface Diff { available: boolean; reason: string; text: string; redacted?: boolean; next_character: number; has_more: boolean }
export interface Check { run_id: string; call_id: string; call_ref?: string; command: string; command_truncated: boolean; cwd: string | null; started_at: string | null; finished_at: string | null; exit_code: number | null; timed_out: boolean; cancelled: boolean; status: string; command_outcome: string; version_status: string; stdout: string; stderr: string; stdout_truncated: boolean; stderr_truncated: boolean; before_scope: Scope | null; after_scope: Scope | null; details_truncated?: boolean; preview?: string }
export interface Drift { state: string; reason?: string; current_scope?: Scope; version_status?: string; checks?: CheckSummary }
export interface EvidenceLink { run_id: string; path?: string; call_id?: string; nonce: number }
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
  'runs.review': { params: { run_id: string }; result: Review };
  'runs.changes': { params: { run_id: string; scope: 'initial' | 'run'; after_file: number; limit: number }; result: Changes };
  'runs.diff': { params: { run_id: string; file_id: string; after_character: number; max_characters: number }; result: Diff };
  'runs.checks': { params: { run_id: string; cursor: string; limit: number }; result: Page<Check> };
  'runs.review_refresh': { params: { run_id: string }; result: Drift };
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
