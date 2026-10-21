import { useEffect, useRef, useState } from 'react';
import type { Change, Changes, Check, Diff, Drift, EvidenceLink, Page, Review, Scope, Workspace } from '../shared';

const api = window.pico;
const errors = (reason: unknown) => reason instanceof Error ? reason.message : String(reason);
const checkLabels: Record<string, string> = { not_run: '未记录验证命令', passed: '已记录命令成功', failed: '命令失败', timed_out: '命令超时', cancelled: '检查已取消', unknown: '结果未知', running: '检查执行中', not_executed: '未执行', stale: '验证版本已过期', inconclusive: '证据不足' };
const reviewLabels: Record<string, string> = { ready: '冻结审查已就绪', partial: '审查证据不完整', unavailable: '审查不可用', pending: '尚未开始采集', collecting: '运行中，正在采集' };
const kinds: Record<string, string> = { created: '新增', modified: '修改', deleted: '删除', unknown: '变化未知' };
const date = (value?: string | null) => value ? new Date(value).toLocaleString() : '未记录';
const binding = (value?: string) => value === 'matched' ? '与观察版本一致' : value === 'stale' ? '观察范围已变化' : '范围不足，无法确认';

function ScopeInfo({ title, value }: { title: string; value: Scope | null }) {
  return <details className="scope-info"><summary>{title} · {value ? `${date(value.captured_at)} · ${value.complete ? '枚举完整' : '枚举不完整'}` : '未捕获'}</summary>
    {value && <><p>范围内 {value.file_count ?? '未知'} 个文件；内容缺失和忽略项仍受下述限制。</p>
      {!!value.notes?.length && <p>采集说明：{value.notes.join('、')}</p>}
      {!!Object.keys(value.content_omissions ?? {}).length && <pre>{JSON.stringify(value.content_omissions, null, 2)}</pre>}</>}</details>;
}

export function ReviewPanel({ workspace, runId, revision, link }: { workspace: Workspace; runId: string; revision: number; link: EvidenceLink | null }) {
  const [review, setReview] = useState<Review | null>(null);
  const [view, setView] = useState<'files' | 'checks'>('files');
  const [error, setError] = useState('');
  const [drift, setDrift] = useState<Drift | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const alive = useRef(true), refreshLock = useRef(false), title = useRef<HTMLHeadingElement>(null);
  const supported = ['run_reviews', 'verification_evidence'].every(c => workspace.info.capabilities?.includes(c));
  const requested = link?.run_id === runId ? link : null;
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    if (!supported) return;
    let active = true;
    void api.request(workspace.handle, 'runs.review', { run_id: runId }).then(value => {
      if (active) { setReview(value); setError(''); }
    }).catch(reason => { if (active) setError(errors(reason)); });
    return () => { active = false; };
  }, [revision]);
  useEffect(() => {
    if (requested) { setView(requested.call_id ? 'checks' : 'files'); title.current?.focus(); }
  }, [requested?.nonce]);
  async function refreshCurrent() {
    if (refreshLock.current) return; refreshLock.current = true; setRefreshing(true); setError('');
    try { const value = await api.request(workspace.handle, 'runs.review_refresh', { run_id: runId });
      if (alive.current) setDrift(value);
    } catch (reason) { if (alive.current) setError(errors(reason)); }
    finally { refreshLock.current = false; if (alive.current) setRefreshing(false); }
  }
  return <aside className="review-pane" aria-label="运行审查" data-testid="review-panel"><div className="review-heading">
    <h2 ref={title} tabIndex={-1}>修改与验证</h2><code>{runId.slice(-8)}</code></div>
    {!supported ? <p className="notice">当前 Host 未声明审查能力，请空闲升级后重连。</p> : <>
      <div className="review-scroll"><p className="badge" data-testid="review-state">{review ? reviewLabels[review.state] ?? review.state : '读取审查记录…'}</p>
        {error && <p role="alert" className="error">{error}</p>}
        {review?.reason && <p className="notice">证据说明：{review.reason}</p>}
        <p className="muted">Diff 比较运行开始与结束时的工作树。开始前已有修改另列；运行期间也可能有外部编辑，不能据此认定唯一作者。</p>
        {review && <><ScopeInfo title="开始范围" value={review.baseline} /><ScopeInfo title="结束范围" value={review.end} /></>}
        <details className="scope-info"><summary>范围与缺失规则</summary><p>Git 忽略项、缓存和构建目录不在捕获范围内；普通目录有界遍历。敏感文件不展示原文，二进制、大文件、链接、不稳定读取或超预算可能没有 Diff。没有 Diff 不代表没有修改。</p></details>
        <div className="frozen-checks" data-testid="frozen-checks"><strong>冻结版本的检查</strong><p>{checkLabels[review?.checks.status ?? 'not_run'] ?? review?.checks.status} · {review?.checks.total ?? 0} 条声明</p>
          <small>只依据真实命令结果；命令成功不代表全部测试通过或覆盖整个仓库。</small></div>
        <section className="drift" aria-label="当前工作树观察"><div className="card-heading"><h3>当前工作树观察</h3><button disabled={refreshing || !review} onClick={() => void refreshCurrent()}>{refreshing ? '观察中…' : '刷新当前漂移'}</button></div>
          {!drift ? <p className="muted">尚未刷新。冻结历史不会自动证明当前文件仍已验证。</p>
            : drift.state === 'busy' ? <p role="status">有活动任务或观察，请稍后刷新；历史证据保持不变。</p>
              : <div data-testid="current-drift"><p>{binding(drift.version_status)} · 观察于 {date(drift.current_scope?.captured_at)}</p>
                <p>该次观察的检查：{checkLabels[drift.checks?.status ?? 'inconclusive']}</p><small>有界、只读的即时观察，之后仍可能变化；没有覆盖冻结历史。</small></div>}</section>
        <div className="review-tabs" role="tablist" aria-label="审查内容"><button role="tab" id="files-tab" aria-controls="files-panel" aria-selected={view === 'files'} onClick={() => setView('files')}>文件差异</button>
          <button role="tab" id="checks-tab" aria-controls="checks-panel" aria-selected={view === 'checks'} onClick={() => setView('checks')}>验证记录</button></div>
        {view === 'files' ? <div role="tabpanel" id="files-panel" aria-labelledby="files-tab"><Files workspace={workspace} runId={runId} revision={revision} review={review} link={requested} /></div>
          : <div role="tabpanel" id="checks-panel" aria-labelledby="checks-tab"><Checks workspace={workspace} runId={runId} revision={revision} link={requested} /></div>}
      </div></>}
  </aside>;
}

function Files({ workspace, runId, revision, review, link }: { workspace: Workspace; runId: string; revision: number; review: Review | null; link: EvidenceLink | null }) {
  const [scope, setScope] = useState<'initial' | 'run'>('run');
  const [after, setAfter] = useState(0);
  const [back, setBack] = useState<number[]>([]);
  const [page, setPage] = useState<Changes | null>(null);
  const [selected, setSelected] = useState<Change | null>(null);
  const [error, setError] = useState('');
  const [seeking, setSeeking] = useState(false);
  const requestToken = useRef(0);
  const sought = useRef<number | undefined>(undefined);
  const [missingLink, setMissingLink] = useState(false);
  useEffect(() => { if (link?.path) { setScope('run'); setAfter(0); setBack([]); } }, [link?.nonce]);
  useEffect(() => {
    const token = ++requestToken.current;
    setError(''); setSeeking(true);
    void (async () => {
      try {
        let offset = after;
        const trail = [...back];
        let data = await api.request(workspace.handle, 'runs.changes', { run_id: runId, scope, after_file: offset, limit: 25 });
        // Explicit tool links search bounded pages, never mount the whole manifest.
        const seek = scope === 'run' && !!link?.path && sought.current !== link.nonce;
        if (seek) {
          for (let i = 0; i < 400 && !data.files.some(f => f.path === link.path) && data.next_file < data.total; i++) {
            if (requestToken.current !== token) return;
            trail.push(offset); offset = data.next_file;
            data = await api.request(workspace.handle, 'runs.changes', { run_id: runId, scope, after_file: offset, limit: 25 });
          }
        }
        if (requestToken.current !== token) return;
        setPage(data);
        if (scope === 'run') {
          const linked = seek ? data.files.find(f => f.path === link?.path) : undefined;
          setSelected(old => linked ?? data.files.find(f => f.id === old?.id) ?? data.files[0] ?? null);
          if (seek) setMissingLink(!linked);
        } else setSelected(null);
        if (seek) sought.current = link?.nonce;
        if (offset !== after) { setBack(trail); setAfter(offset); }
      } catch (reason) { if (requestToken.current === token) setError(errors(reason)); }
      finally { if (requestToken.current === token) setSeeking(false); }
    })();
    return () => { requestToken.current++; };
  }, [scope, after, revision, link?.nonce]);
  const endVersion = `${review?.state}:${review?.end?.captured_at ?? ''}`;
  return <><div className="scope-selector"><label>文件范围<select aria-label="文件范围" value={scope} onChange={e => { setScope(e.target.value as 'initial' | 'run'); setAfter(0); setBack([]); setSelected(null); }}>
    <option value="run">本次运行（{review?.change_count ?? 0}）</option><option value="initial">开始前已有（{review?.initial_change_count ?? 0}）</option></select></label></div>
    {scope === 'initial' && <p className="notice">这些修改在运行开始前已经存在，不计作本次运行的交付。</p>}
    {error && <p role="alert" className="error">{error}</p>}
    {missingLink && <p className="notice">关联路径没有捕获到净差异；可能位于范围外或已恢复原内容。</p>}
    {seeking && <p role="status">读取文件条目…</p>}
    {!seeking && page?.total === 0 && <p className="muted">{scope === 'initial' ? '没有记录开始前的 Git 修改；普通目录不提供 Git 状态。' : review?.end ? '捕获范围内未记录净文件差异；请同时核对范围说明。' : '结束快照尚未捕获，暂不能确认完整差异。'}</p>}
    <ul className="changed-files" aria-label="差异文件">{page?.files.map(f => <li key={f.id ?? f.path}>
      {scope === 'run' ? <button aria-pressed={selected?.id === f.id} onClick={() => setSelected(f)}>{f.path}<small>{kinds[f.kind ?? 'unknown']}{!f.diff_available ? ' · Diff 不可用' : ''}</small></button>
        : <div>{f.path}<small>index: {f.index ?? '—'} · worktree: {f.worktree ?? '—'}（? 为未跟踪）</small></div>}</li>)}</ul>
    {page && <div className="pager"><button disabled={seeking || !back.length} onClick={() => { setAfter(back.at(-1)!); setBack(old => old.slice(0, -1)); }}>上一页文件</button>
      <span>{Math.min(after + 1, page.total)}–{page.next_file} / {page.total}</span><button disabled={seeking || page.next_file >= page.total} onClick={() => { setBack(old => [...old, after]); setAfter(page.next_file); }}>下一页文件</button></div>}
    {scope === 'run' && selected?.id && <FrozenDiff key={`${selected.id}:${endVersion}`} workspace={workspace} runId={runId} file={selected} reveal={selected.path === link?.path ? link.nonce : undefined} />}</>;
}

function FrozenDiff({ workspace, runId, file, reveal }: { workspace: Workspace; runId: string; file: Change; reveal?: number }) {
  const section = useRef<HTMLElement>(null);
  const revealed = useRef<number | undefined>(undefined);
  const [offsets, setOffsets] = useState([0]);
  const [data, setData] = useState<Diff | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true; setData(null); setError('');
    void api.request(workspace.handle, 'runs.diff', { run_id: runId, file_id: file.id!, after_character: offsets.at(-1)!, max_characters: 8192 })
      .then(value => { if (active) setData(value); }).catch(reason => { if (active) setError(errors(reason)); });
    return () => { active = false; };
  }, [offsets]);
  useEffect(() => {
    if (data && reveal !== undefined && revealed.current !== reveal) {
      revealed.current = reveal; section.current?.focus({ preventScroll: true }); section.current?.scrollIntoView({ block: 'start' });
    }
  }, [data, reveal]);
  return <section ref={section} tabIndex={-1} className="frozen-diff" aria-label="冻结文件差异"><h3>{file.path}</h3><p className="muted">冻结 Diff · 开始工作树 → 结束工作树{data?.redacted ? ' · 已脱敏' : ''}</p>
    {error && <p role="alert" className="error">{error}</p>}
    {!data && !error && <p role="status">读取 Diff…</p>}
    {data && !data.available && <p className="notice" data-testid="diff-unavailable">Diff 不可用：{data.reason || file.reason || '证据不完整'}</p>}
    {data?.available && <><pre className="diff-text" tabIndex={0} aria-label="冻结 Diff 文本">{data.text ? data.text.split('\n').map((line, i) => <span key={i} className={line.startsWith('@@') ? 'hunk' : line.startsWith('+') && !line.startsWith('+++') ? 'addition' : line.startsWith('-') && !line.startsWith('---') ? 'deletion' : ''}>{i ? '\n' : ''}{line}</span>) : '该文件没有可展示的文本差异。'}</pre>
      <div className="pager"><button disabled={offsets.length === 1} onClick={() => setOffsets(old => old.slice(0, -1))}>上一段 Diff</button><span>第 {offsets.length} 段</span>
        <button disabled={!data.has_more} onClick={() => setOffsets(old => [...old, data.next_character])}>下一段 Diff</button></div></>}
  </section>;
}

function Checks({ workspace, runId, revision, link }: { workspace: Workspace; runId: string; revision: number; link: EvidenceLink | null }) {
  const [navigation, setNavigation] = useState<{ cursor: string; back: string[]; target?: string }>({ cursor: '', back: [] });
  const [page, setPage] = useState<Page<Check> | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [missingLink, setMissingLink] = useState(false);
  useEffect(() => { if (link?.call_id) setNavigation({ cursor: '', back: [], target: link.call_id }); }, [link?.nonce]);
  useEffect(() => {
    let active = true; setLoading(true); setError('');
    void (async () => {
      try {
        let cursor = navigation.cursor; const back = [...navigation.back];
        let data = await api.request(workspace.handle, 'runs.checks', { run_id: runId, cursor, limit: 25 });
        // At most 128 pages for an explicit link; regular browsing stays one page at a time.
        for (let i = 0; i < 128 && navigation.target && !data.items.some(c => c.call_id === navigation.target) && data.next_cursor; i++) {
          if (!active) return; back.push(cursor); cursor = data.next_cursor;
          data = await api.request(workspace.handle, 'runs.checks', { run_id: runId, cursor, limit: 25 });
        }
        if (!active) return;
        setPage(data);
        if (navigation.target) {
          setMissingLink(!data.items.some(c => c.call_id === navigation.target));
          setNavigation({ cursor, back });
        }
      } catch (reason) { if (active) setError(errors(reason)); }
      finally { if (active) setLoading(false); }
    })();
    return () => { active = false; };
  }, [navigation, revision]);
  return <section aria-label="验证命令证据"><p className="muted">仅列 purpose=verification 的声明。一般命令仍在工具记录中；未声明不自动视作验证。</p>
    {error && <p role="alert" className="error">{error}</p>}{loading && <p role="status">读取检查记录…</p>}
    {missingLink && <p className="notice">未找到关联检查，请继续分页或用 CLI 查看完整记录。</p>}
    {page?.items.length === 0 && <p data-testid="checks-empty">未记录验证命令，不能据模型总结显示“已验证”。</p>}
    {page?.items.map(c => <CheckCard key={c.call_ref ?? c.call_id} check={c} reveal={c.call_id === link?.call_id ? link.nonce : undefined} />)}
    {page && <div className="pager"><button disabled={loading || !navigation.back.length} onClick={() => setNavigation({ cursor: navigation.back.at(-1)!, back: navigation.back.slice(0, -1) })}>上一页检查</button>
      <span>第 {navigation.back.length + 1} 页</span><button disabled={loading || !page.next_cursor} onClick={() => setNavigation({ cursor: page.next_cursor!, back: [...navigation.back, navigation.cursor] })}>下一页检查</button></div>}</section>;
}

function CheckCard({ check: c, reveal }: { check: Check; reveal?: number }) {
  const card = useRef<HTMLElement>(null);
  useEffect(() => {
    if (reveal !== undefined) { card.current?.focus({ preventScroll: true }); card.current?.scrollIntoView({ block: 'start' }); }
  }, [reveal]);
  if (c.details_truncated) return <article ref={card} tabIndex={-1} className="check-card"><h3>检查详情超出展示上限</h3><pre>{c.preview}</pre><p>请用 CLI 导出完整有界记录，不能根据此预览推断验证通过。</p></article>;
  return <article ref={card} tabIndex={-1} className={`check-card ${reveal !== undefined ? 'linked' : ''}`} data-check-id={c.call_id}><div className="card-heading"><h3>{checkLabels[c.status] ?? c.status}</h3><code>exit {c.exit_code ?? '未知'}</code></div>
    <pre>{c.command}</pre>{c.command_truncated && <small>命令展示已截断。</small>}
    <p>原始命令结果：{checkLabels[c.command_outcome] ?? c.command_outcome}；版本绑定：{binding(c.version_status)}</p>
    <details><summary>执行与范围信息</summary><p>调用 {c.call_id}</p><p>工作目录：{c.cwd ?? '未记录'}</p><p>{date(c.started_at)} → {date(c.finished_at)}</p>
      <p>超时：{c.timed_out ? '是' : '否'} · 取消：{c.cancelled ? '是' : '否'}</p><ScopeInfo title="命令前范围" value={c.before_scope} /><ScopeInfo title="命令后范围" value={c.after_scope} /></details>
    <details><summary>stdout{c.stdout_truncated ? '（已截断）' : ''} / stderr{c.stderr_truncated ? '（已截断）' : ''}</summary><h4>stdout</h4><pre>{c.stdout || '（空）'}</pre><h4>stderr</h4><pre>{c.stderr || '（空）'}</pre></details>
  </article>;
}
