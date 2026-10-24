import { useEffect, useState } from 'react';
import type { FilePage, Workspace } from '../shared';
export function FileReader({ workspace, runId, path, line, onClose }: { workspace: Workspace; runId: string; path: string; line: number; onClose: () => void }) {
  const [page, setPage] = useState<FilePage | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [position, setPosition] = useState({ start: line, column: 0 });
  useEffect(() => {
    let alive = true; setBusy(true); setError('');
    if (!workspace.info.capabilities?.includes('file_pages')) { setBusy(false); return; }
    void window.pico.request(workspace.handle, 'workspace.file', { path, start: position.start, end: position.start + 199, column: position.column, ...(runId ? { run_id: runId } : {}) })
      .then(value => { if (alive) setPage(value); }, reason => { if (alive) setError(String(reason)); }).finally(() => { if (alive) setBusy(false); });
    return () => { alive = false; };
  }, [workspace.handle, runId, path, position]);
  return <aside className="review-pane file-reader" aria-label="当前项目文件"><div className="card-heading"><h2>{path}</h2><button onClick={onClose}>关闭文件</button></div>
    <p className="muted">当前文件 · 模型引用需要核对，文件内容可能与历史运行时不同。冻结版本见“修改与验证”。</p>
    {!workspace.info.capabilities?.includes('file_pages') && <p className="notice">本地服务需空闲升级才能浏览文件。</p>}
    {error && <p role="alert" className="error">{error}</p>}
    {page && <><p className="notice">{page.reads?.length ? `所选运行确实读取过 ${page.reads.map(r => `${r.start}–${r.end} 行${r.matches_current ? '（版本相同）' : '（文件已变化）'}`).join('、')}；读取记录只证明返回的范围。` : '所选运行没有此文件的读取记录；模型提及本身不算读取证据。'}</p><p>{page.start}–{page.end} / {page.total_lines} 行{position.column > 0 ? ` · 从字符 ${position.column} 继续` : ''}</p><pre className="diff-text" tabIndex={0}>{page.text}</pre>
      {page.next_line !== null && <button disabled={busy} onClick={() => setPosition({ start: page.next_line!, column: page.next_column })}>继续读取</button>}
      <button disabled={busy} onClick={() => setPosition({ start: line, column: 0 })}>刷新当前文件</button></>}
    {busy && <p role="status">正在读取…</p>}</aside>;
}
