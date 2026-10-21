import { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { compatible, type Approval, type Config, type Configuration, type DesktopAPI, type Message,
  type EvidenceLink, type RecentWorkspace, type Run, type RunItem, type Session, type SessionItem, type Submission, type ToolCall, type Workspace } from '../shared';
import { ReviewPanel } from './review';
import './style.css';

declare global { interface Window { pico: DesktopAPI } }
const api = window.pico;
const terminal = (status: string) => ['completed', 'stopped', 'failed', 'cancelled', 'interrupted'].includes(status);
const statusText: Record<string, string> = { queued: '排队中', running: '运行中', waiting_approval: '等待审批', cancelling: '正在取消', completed: '已完成', stopped: '已停止', failed: '失败', cancelled: '已取消', interrupted: '运行中断', blocked: '工作区已阻塞', ready: '已连接' };
const textError = (reason: unknown) => reason instanceof Error ? reason.message : String(reason);
const connectionFailure = (reason: unknown) => /ECONN|ENOENT|EPIPE|Host disconnected|Host request timed out|Host response|persistence_error/.test(textError(reason));
function merge<T>(old: T[], incoming: T[], id: (item: T) => string) { return [...new Map([...old, ...incoming].map(v => [id(v), v])).values()]; }
function pretty(value: unknown) { return JSON.stringify(value, null, 2); }
function pendingKey(workspace: Workspace) { return `pico:pending:${workspace.workspace}`; }
function pendingFor(workspace: Workspace): Submission | null {
  const value = localStorage.getItem(pendingKey(workspace));
  if (!value) return null;
  try { return JSON.parse(value); } catch { return null; }
}

function App() {
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [recent, setRecent] = useState<RecentWorkspace[]>([]);
  const [busy, setBusy] = useState(true);
  const [disconnected, setDisconnected] = useState(false);
  const [epoch, setEpoch] = useState(0);
  const [error, setError] = useState('');
  const generation = useRef(0);
  async function connect(mode: 'choose' | 'restore' | 'refresh' | 'upgrade' | 'recent', id = '') {
    const token = ++generation.current;
    setBusy(true); setError('');
    try {
      const value = mode === 'choose' ? await api.chooseWorkspace() : mode === 'restore' ? await api.restoreWorkspace()
        : mode === 'recent' ? await api.openRecent(id) : mode === 'upgrade' ? await api.upgradeHost(workspace!.handle)
        : await api.refreshWorkspace(workspace!.handle);
      if (token !== generation.current) return;
      if (value) { setWorkspace(value); setDisconnected(false); setEpoch(n => n + 1); }
      setRecent(await api.recentWorkspaces());
    } catch (reason) { if (token === generation.current) setError(textError(reason)); }
    finally { if (token === generation.current) setBusy(false); }
  }
  useEffect(() => { void connect('restore'); return () => { generation.current++; }; }, []);
  useEffect(() => {
    if (!workspace || !disconnected || busy) return;
    const timer = setTimeout(() => void connect('refresh'), 3000);
    return () => clearTimeout(timer);
  }, [workspace, disconnected, busy]);
  return <div className="shell">
    <aside className="workspaces"><div className="brand"><span className="mark">p</span>Pico</div>
      <p className="caption">工作区</p><button disabled={busy} onClick={() => void connect('choose')}>＋ 选择工作区</button>
      <nav aria-label="最近工作区">{recent.map(r => <button key={r.id} className={workspace?.workspace === r.workspace ? 'selected' : ''}
        disabled={busy} title={r.workspace} onClick={() => void connect('recent', r.id)}>{r.workspace.split('/').at(-1)}</button>)}</nav>
      <div className="sidebar-foot">文件留在本地<br />关闭窗口，任务继续</div></aside>
    <main><header><div><strong>{workspace?.workspace.split('/').at(-1) ?? '工作区'}</strong>
      {workspace && <span className="path" data-testid="workspace-path">{workspace.workspace}</span>}</div>
      <div className="header-actions">{workspace && <><span className="badge">{disconnected ? '连接中断' : statusText[workspace.info.status] ?? workspace.info.status}</span>
        <button disabled={busy} onClick={() => void connect('refresh')}>{busy ? '重连中…' : '刷新连接'}</button>
        <button disabled={busy} onClick={() => void connect('upgrade')}>升级空闲 Host</button></>}</div></header>
      {error && <p role="alert" className="error">{error}</p>}
      {disconnected && <p role="status" className="notice">连接已中断，正在重连。已提交的任务仍以 Host 记录为准。</p>}
      {!workspace ? <section className="welcome"><div className="eyebrow">A LITTLE ROOM TO BUILD</div><h1>从一个工作区开始。</h1>
        <p>选择代码仓库或文件夹，连接你的 Pico。</p><button className="primary" disabled={busy} onClick={() => void connect('choose')}>{busy ? '连接中…' : '选择文件夹 →'}</button></section>
        : !compatible(workspace.info) ? <section className="welcome"><h1>需要升级工作区 Host</h1>
          <p>当前 API {workspace.info.api_version ?? '未知'}，桌面任务控制需要 API 4。</p>
          <p>先完成活动任务。支持原子空闲升级的 Host 可使用上方按钮；更早版本请在 CLI 停止 Host 后刷新连接。</p>
          <code>pico --cwd {'"' + workspace.workspace + '"'} host stop</code></section>
          : <WorkspaceView key={`${workspace.handle}:${workspace.info.instance_id}:${epoch}`} workspace={workspace} onDisconnect={() => setDisconnected(true)} />}
      {workspace && <footer><span>{workspace.info.protocol}</span><span>Host {workspace.info.backend_version} · API {workspace.info.api_version}</span></footer>}
    </main></div>;
}

function WorkspaceView({ workspace, onDisconnect }: { workspace: Workspace; onDisconnect: () => void }) {
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [cursor, setCursor] = useState<string | null>('');
  const [sessionId, setSessionId] = useState('');
  const [config, setConfig] = useState<Configuration | null>(null);
  const [provider, setProvider] = useState('');
  const [model, setModel] = useState('');
  const [approval, setApproval] = useState('ask');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const alive = useRef(true);
  const selected = useRef('');
  const creating = useRef(false);
  const expanded = useRef(false);
  function chooseSession(id: string) {
    selected.current = id; setSessionId(id);
    void api.rememberSelection(workspace.handle, { session_id: id, run_id: '' }).catch(reason => setError(textError(reason)));
  }
  useEffect(() => {
    alive.current = true;
    let inFlight = false;
    const refresh = async () => {
      if (inFlight) return; inFlight = true;
      try {
        const [page, summary] = await Promise.all([api.request(workspace.handle, 'sessions.list', { cursor: '', limit: 30 }), api.request(workspace.handle, 'configuration.get', {})]);
        if (!alive.current) return;
        setSessions(old => merge(page.items, old.filter(s => !page.items.some(n => n.id === s.id)), s => s.id));
        if (!expanded.current) setCursor(page.next_cursor);
        setConfig(summary); setError('');
        if (!selected.current && page.items.length) {
          const saved = pendingFor(workspace)?.session_id || workspace.selection?.session_id;
          selected.current = saved || page.items[0].id; setSessionId(selected.current);
        }
      } catch (reason) { if (alive.current) { setError(textError(reason)); if (connectionFailure(reason)) onDisconnect(); } }
      finally { inFlight = false; }
    };
    void refresh(); const timer = setInterval(() => void refresh(), 2000);
    return () => { alive.current = false; clearInterval(timer); };
  }, []);
  async function more() {
    if (cursor === null || busy) return;
    setBusy(true);
    try { const page = await api.request(workspace.handle, 'sessions.list', { cursor, limit: 30 });
      if (alive.current) { expanded.current = true; setSessions(old => merge(old, page.items, s => s.id).slice(0, 200)); setCursor(page.next_cursor); }
    } catch (reason) { if (alive.current) setError(textError(reason)); }
    finally { if (alive.current) setBusy(false); }
  }
  async function createSession() {
    if (creating.current) return; creating.current = true; setBusy(true); setError('');
    const config: Config = { approval, ...(provider ? { provider } : {}), ...(model.trim() ? { model: model.trim() } : {}) };
    try { const session = await api.request(workspace.handle, 'sessions.create', { config });
      if (alive.current) { setSessions(old => [session, ...old]); chooseSession(session.id); }
    } catch (reason) { if (alive.current) setError(textError(reason)); }
    finally { creating.current = false; if (alive.current) setBusy(false); }
  }
  return <div className="workspace-body"><aside className="sessions"><div className="card-heading"><h2>会话</h2><button disabled={busy} onClick={() => void createSession()}>新建会话</button></div>
    <details><summary>新会话设置</summary><label>提供方<select aria-label="新会话提供方" value={provider} onChange={e => setProvider(e.target.value)}>
      <option value="">项目默认{config ? ` · ${config.provider}` : ''}</option>{['deepseek', 'anthropic', 'openai', 'ollama'].map(p => <option key={p}>{p}</option>)}</select></label>
      <label>模型<input aria-label="新会话模型" value={model} placeholder="项目或提供方默认" onChange={e => setModel(e.target.value)} /></label>
      <label>审批<select aria-label="新会话审批模式" value={approval} onChange={e => setApproval(e.target.value)}><option value="ask">每次询问</option><option value="never">禁止写入</option><option value="auto">自动批准</option></select></label></details>
    <nav aria-label="会话列表">{sessions.map(s => <button key={s.id} aria-current={s.id === sessionId ? 'true' : undefined} onClick={() => chooseSession(s.id)}>
      <span>{s.id.slice(-8)}</span><small>{s.provider_protocol} · {new Date(s.created_at).toLocaleDateString()}</small></button>)}</nav>
    {cursor && <button disabled={busy || sessions.length >= 200} onClick={() => void more()}>更多会话</button>}
    {!sessions.length && <p className="muted">新建会话，开始第一个任务。</p>}
    {error && <p role="alert" className="error">{error}</p>}</aside>
    {sessionId ? <SessionView key={sessionId} workspace={workspace} sessionId={sessionId} onDisconnect={onDisconnect} />
      : <section className="welcome"><h1>准备开始。</h1><p>会话与运行记录保存在当前工作区。</p></section>}</div>;
}

function SessionView({ workspace, sessionId, onDisconnect }: { workspace: Workspace; sessionId: string; onDisconnect: () => void }) {
  const [session, setSession] = useState<Session | null>(null);
  const [config, setConfig] = useState<Configuration | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [runs, setRuns] = useState<RunItem[]>([]);
  const [runCursor, setRunCursor] = useState<string | null>('');
  const [runId, setRunId] = useState('');
  const [prompt, setPrompt] = useState('');
  const [approval, setApproval] = useState('ask');
  const [pending, setPending] = useState<Submission | null>(() => pendingFor(workspace));
  const [sending, setSending] = useState(false);
  const [submissionError, setSubmissionError] = useState('');
  const [view, setView] = useState<'conversation' | 'review'>('conversation');
  const [reviewVersion, setReviewVersion] = useState({ run_id: '', sequence: 0 });
  const [evidence, setEvidence] = useState<EvidenceLink | null>(null);
  const [error, setError] = useState('');
  const alive = useRef(true), sendLock = useRef(false), historyLoading = useRef(false);
  const historyNext = useRef(0), historyTotal = useRef(0), selectedRun = useRef('');
  const historyRevision = useRef<string | undefined>(undefined);
  const expandedRuns = useRef(false), initializedApproval = useRef(false);
  const refresh = useRef<() => Promise<void>>(async () => {});
  const selectRun = (id: string) => {
    selectedRun.current = id; setRunId(id);
    void api.rememberSelection(workspace.handle, { session_id: sessionId, run_id: id }).catch(() => {});
  };
  useEffect(() => {
    alive.current = true;
    let inFlight = false;
    refresh.current = async () => {
      if (inFlight) return; inFlight = true;
      try {
        const [head, page, summary] = await Promise.all([
          api.request(workspace.handle, 'sessions.get', { session_id: sessionId, after_message: 0, limit: 30 }),
          api.request(workspace.handle, 'runs.list', { cursor: '', limit: 30, session_id: sessionId }),
          api.request(workspace.handle, 'configuration.get', { session_id: sessionId }),
        ]);
        if (!alive.current) return;
        setSession(head); setConfig(summary); setError('');
        setPending(pendingFor(workspace));
        if (!initializedApproval.current) { initializedApproval.current = true; setApproval(head.config.approval ?? 'ask'); }
        const reset = historyRevision.current !== head.history_revision;
        historyRevision.current = head.history_revision;
        if (reset) { historyNext.current = 0; historyTotal.current = 0; }
        setMessages(old => merge(reset ? [] : old.filter(m => m.message_index < head.history_count), head.history, m => m.message_id).sort((a, b) => a.message_index - b.message_index).slice(-500));
        const caughtUp = historyTotal.current > 0 && historyNext.current >= historyTotal.current;
        historyNext.current = head.history_count < historyTotal.current ? head.next_message : Math.max(historyNext.current, head.next_message);
        historyTotal.current = head.history_count;
        if (caughtUp && head.history_count > historyNext.current && !historyLoading.current) {
          historyLoading.current = true;
          try { const tail = await api.request(workspace.handle, 'sessions.get', { session_id: sessionId, after_message: historyNext.current, limit: 30 });
            if (alive.current) { setMessages(old => merge(old, tail.history, m => m.message_id).sort((a, b) => a.message_index - b.message_index).slice(-500)); historyNext.current = tail.next_message; }
          } finally { historyLoading.current = false; }
        }
        setRuns(old => merge(page.items.filter(r => !r.parent_run_id), old.filter(r => !page.items.some(n => n.run_id === r.run_id)), r => r.run_id));
        if (!expandedRuns.current) setRunCursor(page.next_cursor);
        if (!selectedRun.current) {
          const saved = workspace.selection?.session_id === sessionId ? workspace.selection.run_id : '';
          const first = page.items.find(r => !r.parent_run_id);
          if (saved || first) selectRun(saved || first!.run_id);
        }
      } catch (reason) { if (alive.current) { setError(textError(reason)); if (connectionFailure(reason)) onDisconnect(); } }
      finally { inFlight = false; }
    };
    void refresh.current(); const timer = setInterval(() => void refresh.current(), 2000);
    return () => { alive.current = false; clearInterval(timer); };
  }, []);
  async function moreHistory() {
    if (historyLoading.current) return; historyLoading.current = true;
    try { const page = await api.request(workspace.handle, 'sessions.get', { session_id: sessionId, after_message: historyNext.current, limit: 30 });
      if (alive.current) { setMessages(old => merge(old, page.history, m => m.message_id).sort((a, b) => a.message_index - b.message_index).slice(-500)); historyNext.current = page.next_message; }
    } catch (reason) { if (alive.current) setError(textError(reason)); }
    finally { historyLoading.current = false; }
  }
  async function moreRuns() {
    if (!runCursor) return;
    try { const page = await api.request(workspace.handle, 'runs.list', { cursor: runCursor, limit: 30, session_id: sessionId });
      if (alive.current) { expandedRuns.current = true; setRuns(old => merge(old, page.items.filter(r => !r.parent_run_id), r => r.run_id).slice(0, 200)); setRunCursor(page.next_cursor); }
    } catch (reason) { if (alive.current) setError(textError(reason)); }
  }
  async function submit(retry = false) {
    if (sendLock.current || (retry && pending?.session_id !== sessionId) || (!retry && (pending || !prompt.trim()))) return;
    sendLock.current = true; setSending(true); setSubmissionError('');
    const intent = retry ? pending! : { session_id: sessionId, prompt: prompt.trim(), client_request_id: crypto.randomUUID(), config: { approval } };
    try {
      // Durable transport intent precedes the first mutation. Retries never mint a new request ID.
      localStorage.setItem(pendingKey(workspace), JSON.stringify(intent)); setPending(intent);
      const run = await api.request(workspace.handle, 'runs.start', intent);
      localStorage.removeItem(pendingKey(workspace));
      if (alive.current) { setPending(null); setPrompt(''); selectRun(run.run_id); await refresh.current(); }
    } catch (reason) {
      // These errors are explicit pre-submission rejections, unlike an uncertain transport failure.
      if (/(invalid_request|unsupported_protocol|unsupported_session|session_too_large|workspace_blocked|workspace_busy|configuration_error|not_found):/.test(textError(reason))) {
        localStorage.removeItem(pendingKey(workspace));
        if (alive.current) setPending(null);
      }
      if (alive.current) setSubmissionError(textError(reason));
    } finally { sendLock.current = false; if (alive.current) setSending(false); }
  }
  const active = runs.some(r => !terminal(r.status));
  function openEvidence(value: Omit<EvidenceLink, 'nonce'>) { setEvidence({ ...value, nonce: Date.now() }); setView('review'); }
  return <div className="session-stage"><div className="session-views" aria-label="会话视图"><button aria-pressed={view === 'conversation'} onClick={() => setView('conversation')}>对话与运行</button>
    <button aria-pressed={view === 'review'} onClick={() => setView('review')}>修改与验证</button></div>
    <div className="session-layout" data-view={view}><div className="conversation"><div className="session-heading"><h2>会话 {sessionId.slice(-8)}</h2>
    <p>{config?.provider} · {config?.model} · <span>{session?.provider_protocol}</span></p>
    <details><summary>模型连接</summary><p>{config?.endpoint}</p><p>{config?.api_key_configured ? '密钥已配置' : config?.provider === 'ollama' ? '本地模型无需 API 密钥' : '未配置密钥。如服务需要认证，请在工作区 .env 配置对应 PICO_*_API_KEY，然后刷新连接。'}</p></details></div>
    {error && <p role="alert" className="error">{error}</p>}
    <div className="conversation-scroll"><section className="history" aria-label="会话历史">{messages.filter(m => m.role !== 'tool' && (m.content || !(m.tool_calls?.length || m.tool_call_count))).map(m =>
      <article className={`message ${m.role}`} key={m.message_id} data-message-id={m.message_id}><small>{m.role === 'user' ? '你' : 'Pico'}</small><pre>{m.content}</pre>{m.content_truncated && <small>消息已截断，完整记录可由 CLI 导出。</small>}</article>)}
      {session && historyNext.current < session.history_count && <button onClick={() => void moreHistory()}>继续加载历史</button>}
      {messages.length >= 500 && <p className="muted">保留最近加载的 500 条消息。</p>}</section>
      <section className="runs" aria-label="运行记录"><div className="card-heading"><h3>运行记录</h3><select aria-label="选择运行" value={runId} onChange={e => selectRun(e.target.value)}>
        {!runId && <option value="">尚无运行</option>}{runs.map(r => <option key={r.run_id} value={r.run_id}>{r.run_id.slice(-8)} · {statusText[r.status] ?? r.status}</option>)}</select>
        {runCursor && <button disabled={runs.length >= 200} onClick={() => void moreRuns()}>更多运行</button>}</div>
        {runId && <RunPanel key={runId} workspace={workspace} runId={runId} messages={messages} onDisconnect={onDisconnect}
          onEvidence={openEvidence} onState={state => {
            setRuns(old => old.some(r => r.run_id === state.run_id) ? old.map(r => r.run_id === state.run_id ? { ...r, status: state.status } : r) : [state, ...old]);
            setReviewVersion(old => old.run_id === state.run_id && old.sequence === state.last_sequence ? old : { run_id: state.run_id, sequence: state.last_sequence });
          }} />}</section></div>
    <form className="composer" onSubmit={e => { e.preventDefault(); void submit(); }}>
      {submissionError && <p role="alert" className="error">{submissionError}</p>}
      <label htmlFor="prompt">{active ? '继续发送会排队，由 Host 顺序执行' : '发送任务'}</label>
      <textarea id="prompt" value={prompt} onChange={e => setPrompt(e.target.value)} placeholder="描述你想完成的事…" disabled={sending || !!pending}
        onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !e.nativeEvent.isComposing && e.nativeEvent.keyCode !== 229) { e.preventDefault(); void submit(); } }} />
      <div className="composer-actions"><label>本次审批<select aria-label="本次审批模式" value={approval} onChange={e => setApproval(e.target.value)} disabled={sending || !!pending}>
        <option value="ask">每次询问</option><option value="never">禁止写入</option><option value="auto">自动批准</option></select></label>
        {pending ? <button type="button" disabled={sending || pending.session_id !== sessionId} onClick={() => void submit(true)}>{sending ? '正在确认…' : '重试确认同一次发送'}</button>
          : <button className="primary" type="submit" disabled={sending || !prompt.trim()}>{sending ? '发送中…' : active ? '加入队列' : '发送任务'}</button>}</div>
      {pending && <p className="notice">会话 {pending.session_id.slice(-8)} 的发送结果待确认，请在该会话重试确认。将复用原请求 ID；原任务可能已经受理。</p>}
    </form></div>{runId ? <ReviewPanel key={runId} workspace={workspace} runId={runId} revision={reviewVersion.run_id === runId ? reviewVersion.sequence : 0} link={evidence} />
      : <aside className="review-pane"><h2>修改与验证</h2><p className="muted">选择运行后查看冻结差异和验证证据。</p></aside>}</div></div>;
}

function RunPanel({ workspace, runId, messages, onDisconnect, onState, onEvidence }: { workspace: Workspace; runId: string; messages: Message[]; onDisconnect: () => void; onState: (run: Run) => void; onEvidence: (link: Omit<EvidenceLink, 'nonce'>) => void }) {
  const [run, setRun] = useState<Run | null>(null);
  const [calls, setCalls] = useState<ToolCall[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [toolPages, setToolPages] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [activity, setActivity] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [actionError, setActionError] = useState('');
  const alive = useRef(true), mutation = useRef(false);
  const pages = useRef(1);
  const refresh = useRef<() => Promise<void>>(async () => {});
  useEffect(() => {
    alive.current = true;
    let inFlight = false;
    refresh.current = async () => {
      if (inFlight) return; inFlight = true;
      try {
        const [state, pending] = await Promise.all([api.request(workspace.handle, 'runs.get', { run_id: runId }), api.request(workspace.handle, 'approvals.list', {})]);
        let cursor = '', tools: ToolCall[] = [], more = false;
        for (let page = 0; page < pages.current; page++) {
          const result = await api.request(workspace.handle, 'runs.tool_calls', { run_id: runId, cursor, limit: 30 });
          tools = [...tools, ...result.items]; more = !!result.next_cursor;
          if (!result.next_cursor) break; cursor = result.next_cursor;
        }
        if (alive.current) { setRun(state); onState(state); setCalls(tools); setHasMore(more); setApprovals(pending.filter(a => a.run_id === runId)); setError(''); }
      } catch (reason) { if (alive.current) { setError(textError(reason)); if (connectionFailure(reason)) onDisconnect(); } }
      finally { inFlight = false; }
    };
    void refresh.current(); const timer = setInterval(() => void refresh.current(), 2000);
    return () => { alive.current = false; clearInterval(timer); };
  }, []);
  useEffect(() => {
    const id = crypto.randomUUID();
    let active = true;
    const dispose = api.onActivity(event => {
      if (!active || event.subscription_id !== id || event.handle !== workspace.handle || event.run_id !== runId) return;
      if (event.error) { setError(event.error); onDisconnect(); return; }
      const latest = event.events.at(-1);
      const labels: Record<string, string> = { model_requested: '等待模型响应', model_completed: '模型已响应', tool_started: '工具执行中', tool_executed: '工具结果已记录', approval_requested: '等待你的审批', cancel_requested: '取消请求已发送' };
      if (latest && labels[latest.event]) setActivity(labels[latest.event]);
      void refresh.current();
    });
    void api.watchRun(workspace.handle, id, runId, 0).catch(reason => { if (active) { setError(textError(reason)); onDisconnect(); } });
    return () => { active = false; dispose(); void api.stopWatch(id).catch(() => {}); };
  }, []);
  async function act(id: string, approved?: boolean) {
    if (mutation.current) return; mutation.current = true; setBusy(id); setActionError('');
    try {
      if (approved === undefined) await api.request(workspace.handle, 'runs.cancel', { run_id: runId });
      else await api.request(workspace.handle, 'approvals.resolve', { approval_id: id, approved });
    } catch (reason) { if (alive.current) setActionError(textError(reason)); }
    finally { mutation.current = false; if (alive.current) { setBusy(''); void refresh.current(); } }
  }
  return <div className="run-panel" data-testid="run-panel"><div className="card-heading"><code data-testid="run-id">{runId}</code>
    <span className={`badge ${run?.status}`} data-testid="run-status">{run ? statusText[run.status] ?? run.status : '读取中'}</span>
    {run && !terminal(run.status) && <button disabled={!!busy || run.status === 'cancelling'} onClick={() => void act('cancel')}>{busy === 'cancel' || run.status === 'cancelling' ? '正在请求取消…' : '取消运行'}</button>}</div>
    {run && !terminal(run.status) && <p role="status" className="muted">{run.status === 'cancelling' ? '取消请求已发送，等待 Host 确认终态。' : activity || '等待 Host 活动；模型响应按实际记录显示。'}</p>}
    {(run?.status === 'interrupted' || !!run?.report?.unknown_calls?.length) && <p className="notice">部分操作结果未知。请先检查工作区文件，再决定是否提交新任务。</p>}
    {run?.stop_reason && terminal(run.status) && <p className="muted">{run.stop_reason}</p>}
    {error && <p role="alert" className="error">{error}</p>}
    {actionError && <p role="alert" className="error">{actionError}。已刷新 Host 审批与运行状态。</p>}
    {approvals.map(a => <div className="approval" key={a.id} data-testid="approval-card"><h3>批准 {a.name}？</h3>
      <pre>{pretty(a.args)}</pre>{a.args_truncated && <p>参数展示已截断；批准的是原始完整调用。</p>}
      <p>作用范围：{a.scope.cwd}{a.scope.path !== '.' ? ` / ${a.scope.path}` : '（整个工作区）'}</p>
      <p>这是操作范围，不是精确写入预览。</p><p>有效至 {new Date(a.expires_at).toLocaleString()}</p>
      <div className="actions"><button className="primary" disabled={!!busy} onClick={() => void act(a.id, true)}>批准</button><button disabled={!!busy} onClick={() => void act(a.id, false)}>拒绝</button>{busy === a.id && <span>等待 Host 确认…</span>}</div></div>)}
    {calls.map(c => <details className="tool-card" key={c.call_ref ?? c.call_id} data-call-id={c.call_ref ?? c.call_id}><summary><strong>{c.name}</strong><span>{c.result?.status ?? c.phase}</span></summary>
      <pre>{c.details_truncated ? c.preview : pretty({ args: c.args, result: c.result })}</pre>{c.details_truncated && <p>工具详情已截断，可用 CLI 导出完整有界执行记录。</p>}
      <div className="evidence-links">{c.result?.metadata?.affected_paths?.slice(0, 20).map(path => <button key={path} onClick={() => onEvidence({ run_id: runId, path })}>查看文件 {path}</button>)}
        {c.name === 'run_shell' && c.result?.data?.purpose === 'verification' && <button onClick={() => onEvidence({ run_id: runId, call_id: c.call_id })}>查看检查记录</button>}</div></details>)}
    {hasMore && <button disabled={toolPages >= 4} onClick={() => { pages.current++; setToolPages(pages.current); void refresh.current(); }}>更多工具记录</button>}
    {run?.final_answer && !messages.some(m => m.role === 'assistant' && m.content === run.final_answer) && <article className="message assistant"><small>本次运行结果</small><pre>{run.final_answer}</pre></article>}
  </div>;
}
createRoot(document.getElementById('root')!).render(<App />);
