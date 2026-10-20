import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import type { DesktopAPI, Workspace } from '../shared';
import './style.css';

declare global { interface Window { pico: DesktopAPI } }

function App() {
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function connect(refresh = false) {
    setBusy(true); setError('');
    try {
      const value = refresh && workspace ? await window.pico.refreshWorkspace(workspace.handle) : await window.pico.chooseWorkspace();
      if (value) setWorkspace(value);
    } catch (reason) { setError(reason instanceof Error ? reason.message : '连接失败，请重试'); }
    finally { setBusy(false); }
  }
  return <div className="shell">
    <aside><div className="brand"><span className="mark">p</span>Pico</div>
      <p className="caption">你的本地工作区</p>
      <button className="workspace-button" disabled={busy} onClick={() => void connect()}>＋ 选择工作区</button>
      {workspace && <div className="selected"><span className="dot" />{workspace.workspace.split('/').at(-1)}</div>}
      <div className="sidebar-foot">文件留在本地<br />任务在后台继续</div>
    </aside>
    <main><header><span>工作区</span><span className="local">● 本地连接</span></header>
      <section><div className="eyebrow">A LITTLE ROOM TO BUILD</div>
        <h1>{workspace ? '工作区已连接。' : '从一个工作区开始。'}</h1>
        <p className="intro">{workspace ? '窗口可以关闭。运行中的任务仍由本地 Host 管理。' : '选择代码仓库或文件夹，连接你的 Pico。'}</p>
        {workspace ? <div className="connection" aria-live="polite">
          <div className="card-heading"><span>连接详情</span><span className="badge">{workspace.info.status}</span></div>
          <dl><dt>工作目录</dt><dd data-testid="workspace-path">{workspace.workspace}</dd>
            <dt>后端版本</dt><dd>{workspace.info.backend_version ?? '旧版本（未提供）'}</dd>
            <dt>通信协议</dt><dd>{workspace.info.protocol}</dd>
            <dt>可用能力</dt><dd>{workspace.info.capabilities?.join(' · ') ?? 'Host 未声明能力'}</dd></dl>
          <button disabled={busy} onClick={() => void connect(true)}>{busy ? '连接中…' : '刷新连接'}</button>
        </div> : <button className="primary" disabled={busy} onClick={() => void connect()}>{busy ? '连接中…' : '选择文件夹 →'}</button>}
        {error && <p role="alert" className="error">{error}</p>}
        <p className="note">连接与断开不会修改仓库文件。需要结束任务时，请显式取消。</p>
      </section><footer>PICO <span>轻量、可观察、由你掌控。</span></footer>
    </main>
  </div>;
}
createRoot(document.getElementById('root')!).render(<App />);
