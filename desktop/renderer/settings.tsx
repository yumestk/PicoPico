import { useEffect, useRef, useState } from 'react';
import type { ModelSettings } from '../shared';

export function Settings({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const modal = useRef<HTMLDialogElement>(null);
  const [settings, setSettings] = useState<ModelSettings | null>(null);
  const [provider, setProvider] = useState('deepseek');
  const [model, setModel] = useState('');
  const [endpoint, setEndpoint] = useState('');
  const [key, setKey] = useState('');
  const [clear, setClear] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const alive = useRef(true);
  function choose(value: ModelSettings, name = value.provider) {
    setSettings(value); setProvider(name); setModel(value.profiles[name]?.model ?? '');
    setEndpoint(value.profiles[name]?.endpoint ?? ''); setKey(''); setClear(false);
  }
  useEffect(() => {
    modal.current?.showModal();
    void window.pico.modelSettings().then(value => { if (alive.current) choose(value); })
      .catch(reason => { if (alive.current) setError(String(reason)); });
    return () => { alive.current = false; };
  }, []);
  async function save() {
    if (busy || !settings) return; setBusy(true); setError('');
    try {
      await window.pico.saveModelSettings({ provider, model, endpoint, ...(key ? { api_key: key } : {}), clear_api_key: clear });
      if (alive.current) { setKey(''); onSaved(); }
    } catch (reason) { if (alive.current) setError(String(reason)); }
    finally { if (alive.current) setBusy(false); }
  }
  async function importExisting() {
    if (busy) return; setBusy(true); setError('');
    try { const value = await window.pico.importModelSettings();
      if (value && alive.current) { choose(value); onSaved(); }
    } catch (reason) { if (alive.current) setError(String(reason)); }
    finally { if (alive.current) setBusy(false); }
  }
  return <dialog ref={modal} className="model-settings" aria-labelledby="model-settings-title" onCancel={event => { if (busy) event.preventDefault(); }} onClose={onClose}>
    <h2 id="model-settings-title">Pico 模型设置</h2><p>配置一次，所有项目共用。选择项目只决定 Pico 读取和处理哪些文件。</p>
    <form onSubmit={event => { event.preventDefault(); void save(); }}>
      <label>提供方<select aria-label="Pico 提供方" disabled={busy || !settings} value={provider} onChange={event => choose(settings!, event.target.value)}>
        {['deepseek', 'openai', 'anthropic', 'ollama'].map(name => <option key={name}>{name}</option>)}</select></label>
      <label>模型<input aria-label="Pico 模型" value={model} placeholder="提供方默认模型" disabled={busy} onChange={event => setModel(event.target.value)} /></label>
      <label>服务地址<input aria-label="Pico 服务地址" type="url" value={endpoint} placeholder="留空使用提供方默认地址" disabled={busy} onChange={event => setEndpoint(event.target.value)} /></label>
      {provider !== 'ollama' && <><label>API key<input aria-label="Pico API key" type="password" autoComplete="new-password" value={key} disabled={busy || clear}
        placeholder={settings?.profiles[provider]?.api_key_configured ? '已保存；留空保留，输入可替换' : '填写此提供方的 API key'} onChange={event => setKey(event.target.value)} /></label>
        {settings?.profiles[provider]?.api_key_configured && <label className="clear-key"><input type="checkbox" checked={clear} disabled={busy} onChange={event => { setClear(event.target.checked); setKey(''); }} />清除已保存密钥</label>}</>}
      <p className="muted">设置保存在本机 Pico 配置中。保存后新会话使用这里的模型，已有会话保留原模型。</p>
      {error && <p role="alert" className="error">{error}</p>}
      <div className="settings-actions"><button type="button" disabled={busy} onClick={() => void importExisting()}>从已有 .env 导入</button>
        <button type="button" disabled={busy} onClick={() => modal.current?.close()}>取消</button>
        <button className="primary" disabled={busy || !settings} type="submit">{busy ? '保存中…' : '保存设置'}</button></div>
    </form>
  </dialog>;
}
