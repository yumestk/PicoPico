import { app, BrowserWindow, dialog, ipcMain, protocol, session } from 'electron';
import { execFile } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { readFile, writeFile, rename } from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';
import { callHost, hostInfo } from './host-client';
import type { Activity, Run, RunEvent, Selection, Workspace } from '../shared';

protocol.registerSchemesAsPrivileged([{ scheme: 'pico', privileges: { standard: true, secure: true, supportFetchAPI: true } }]);
const execute = promisify(execFile);
const workspaces = new Map<string, { workspace: string; socket: string }>();
const watches = new Map<string, AbortController>();
let window: BrowserWindow | null;
let requests = 0;
const PAGE = 'pico://app/index.html';
interface Recent { id: string; workspace: string; selection?: Selection }
interface Preferences { recent: Recent[]; last?: string }
let preferences: Preferences;
let saving = Promise.resolve();

async function prefs() {
  if (!preferences) {
    try {
      const raw = await readFile(path.join(app.getPath('userData'), 'workspaces.json'), 'utf8');
      if (raw.length > 65536) throw new Error('Workspace preferences exceed size limit');
      const value = JSON.parse(raw);
      preferences = { recent: Array.isArray(value.recent) ? value.recent.filter((r: Recent) =>
        typeof r.id === 'string' && typeof r.workspace === 'string' && path.isAbsolute(r.workspace)).slice(0, 8) : [], last: value.last };
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
      preferences = { recent: [] };
    }
  }
  return preferences;
}
function savePrefs() {
  const body = JSON.stringify(preferences);
  saving = saving.catch(() => {}).then(async () => {
    const file = path.join(app.getPath('userData'), 'workspaces.json');
    await writeFile(file + '.tmp', body, { mode: 0o600 });
    await rename(file + '.tmp', file);
  });
  return saving;
}
function stopWatches() { for (const controller of watches.values()) controller.abort(); watches.clear(); }
function checkSender(event: Electron.IpcMainInvokeEvent) {
  if (!window || event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame
    || event.senderFrame.url !== PAGE) throw new Error('Untrusted desktop sender');
}
function selected(handle: unknown) {
  const value = typeof handle === 'string' ? workspaces.get(handle) : undefined;
  if (!value) throw new Error('Unknown workspace');
  return value;
}
function identifier(value: unknown): asserts value is string {
  if (typeof value !== 'string' || !value || value.length > 200) throw new Error('Invalid identifier');
}
async function openWorkspace(directory: string, upgrade = false): Promise<Workspace> {
  const backend = app.isPackaged ? path.join(process.resourcesPath, 'backend') : path.join(app.getAppPath(), 'runtime/backend');
  const python = path.join(backend, 'python/bin/python3');
  let stdout: string;
  try {
    ({ stdout } = await execute(python, ['-I', '-B', '-m', 'pico.desktop_bootstrap', '--workspace', directory, ...(upgrade ? ['--upgrade'] : [])], {
      cwd: backend, timeout: 25000, maxBuffer: 65536,
      env: { ...process.env, SSL_CERT_FILE: path.join(backend, 'cacert.pem') },
    }));
  } catch (error) {
    const output = (error as { stdout?: string }).stdout;
    if (output) { const parsed = JSON.parse(output); throw new Error(`${parsed.error?.code}: ${parsed.error?.message}`); }
    throw new Error('Host 启动失败，请检查工作区 .pico/host.log');
  }
  const value = JSON.parse(stdout);
  if (typeof value.workspace !== 'string' || typeof value.socket !== 'string'
    || !path.isAbsolute(value.workspace) || !path.isAbsolute(value.socket)) throw new Error('Invalid workspace bootstrap');
  const info = await hostInfo(value.socket, value.workspace);
  const known = [...workspaces.entries()].find(([, w]) => w.workspace === value.workspace);
  const handle = known?.[0] ?? randomUUID();
  workspaces.set(handle, { workspace: value.workspace, socket: value.socket });
  const state = await prefs();
  const recent: Recent = state.recent.find(r => r.workspace === value.workspace) ?? { id: randomUUID(), workspace: value.workspace };
  state.recent = [recent, ...state.recent.filter(r => r !== recent)].slice(0, 8);
  state.last = recent.id;
  await savePrefs();
  return { handle, workspace: value.workspace, info, selection: recent.selection };
}

// Only application use cases cross the bridge; socket paths, files, private IPC and host.stop do not.
const methods: Record<string, string[]> = {
  'configuration.get': ['session_id'], 'sessions.list': ['cursor', 'limit'], 'sessions.get': ['session_id', 'after_message', 'limit'],
  'sessions.create': ['config'], 'runs.list': ['cursor', 'limit', 'session_id'], 'runs.get': ['run_id'],
  'runs.start': ['session_id', 'prompt', 'client_request_id', 'config'], 'runs.cancel': ['run_id'],
  'runs.tool_calls': ['run_id', 'cursor', 'limit'], 'approvals.list': [], 'approvals.resolve': ['approval_id', 'approved'],
  'runs.review': ['run_id'], 'runs.changes': ['run_id', 'scope', 'after_file', 'limit'],
  'runs.diff': ['run_id', 'file_id', 'after_character', 'max_characters'],
  'runs.checks': ['run_id', 'cursor', 'limit'], 'runs.review_refresh': ['run_id'],
};

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => { window?.show(); window?.focus(); });
  app.whenReady().then(() => {
    protocol.handle('pico', async request => {
      const url = new URL(request.url);
      const name = decodeURIComponent(url.pathname).slice(1);
      if (url.host !== 'app' || !(name === 'index.html' || /^assets\/[A-Za-z0-9_.-]+$/.test(name))) return new Response('Not found', { status: 404 });
      const content = await readFile(path.join(__dirname, '../renderer', name));
      const types: Record<string, string> = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' };
      return new Response(content, { headers: { 'Content-Type': types[path.extname(name)] ?? 'application/octet-stream' } });
    });
    session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    session.defaultSession.webRequest.onBeforeRequest((details, callback) => callback({ cancel: !details.url.startsWith('pico://app/') }));
    ipcMain.handle('workspace:choose', async event => {
      checkSender(event);
      const result = await dialog.showOpenDialog(window!, { title: '选择 Pico 工作区', properties: ['openDirectory'] });
      return result.canceled ? null : openWorkspace(result.filePaths[0]);
    });
    ipcMain.handle('workspace:recent', async event => { checkSender(event); return (await prefs()).recent.map(({ id, workspace }) => ({ id, workspace })); });
    ipcMain.handle('workspace:restore', async event => {
      checkSender(event); const state = await prefs(); const recent = state.recent.find(r => r.id === state.last);
      return recent ? openWorkspace(recent.workspace) : null;
    });
    ipcMain.handle('workspace:openRecent', async (event, id: unknown) => {
      checkSender(event); const recent = (await prefs()).recent.find(r => r.id === id);
      if (!recent) throw new Error('Unknown recent workspace');
      return openWorkspace(recent.workspace);
    });
    ipcMain.handle('workspace:selection', async (event, handle: unknown, selection: Selection) => {
      checkSender(event); const target = selected(handle);
      if (!selection || typeof selection.session_id !== 'string' || typeof selection.run_id !== 'string'
        || selection.session_id.length > 200 || selection.run_id.length > 200) throw new Error('Invalid selection');
      const recent = (await prefs()).recent.find(r => r.workspace === target.workspace);
      if (recent) { recent.selection = selection; await savePrefs(); }
    });
    ipcMain.handle('workspace:refresh', async (event, handle: unknown) => {
      checkSender(event); return openWorkspace(selected(handle).workspace);
    });
    ipcMain.handle('workspace:upgrade', async (event, handle: unknown) => {
      checkSender(event); return openWorkspace(selected(handle).workspace, true);
    });
    ipcMain.handle('host:request', async (event, handle: unknown, method: unknown, params: Record<string, unknown>) => {
      checkSender(event); const target = selected(handle);
      if (typeof method !== 'string' || !Object.hasOwn(methods, method) || !params || Array.isArray(params)
        || Object.keys(params).some(k => !methods[method].includes(k))) throw new Error('Invalid desktop request');
      if (params.config && (typeof params.config !== 'object' || Array.isArray(params.config)
        || Object.keys(params.config).some(k => !['provider', 'model', 'approval'].includes(k)))) throw new Error('Invalid desktop configuration');
      if (requests >= 16) throw new Error('Too many desktop requests');
      requests++;
      try { return await callHost(target.socket, target.workspace, method, params); }
      finally { requests--; }
    });
    ipcMain.handle('host:unwatch', (event, id: unknown) => {
      checkSender(event); if (typeof id === 'string') { watches.get(id)?.abort(); watches.delete(id); }
    });
    ipcMain.handle('host:watch', async (event, handle: unknown, id: unknown, runId: unknown, after: unknown) => {
      checkSender(event); const target = selected(handle);
      identifier(id); identifier(runId);
      if (typeof after !== 'number' || !Number.isSafeInteger(after) || after < 0) throw new Error('Invalid event cursor');
      stopWatches(); const controller = new AbortController(); watches.set(id, controller);
      const { signal } = controller;
      const send = (value: Partial<Activity>) => {
        if (!signal.aborted && window && !window.isDestroyed()) window.webContents.send('host:activity', {
          subscription_id: id, handle, run_id: runId, events: [], next_sequence: after, ...value,
        });
      };
      void (async () => {
        let cursor = after;
        try {
          while (!signal.aborted) {
            const page = await callHost<{ events: RunEvent[]; next_sequence: number }>(target.socket, target.workspace,
              'runs.events', { run_id: runId, after_sequence: cursor, limit: 50, wait_ms: 10000 }, 15000, signal);
            cursor = page.next_sequence;
            send(page);
            const run = await callHost<Run>(target.socket, target.workspace, 'runs.get', { run_id: runId }, 5000, signal);
            if (['completed', 'stopped', 'failed', 'cancelled', 'interrupted'].includes(run.status) && cursor >= run.last_sequence) break;
          }
        } catch (error) { if (!signal.aborted) send({ error: String(error), next_sequence: cursor }); }
        finally { if (watches.get(id) === controller) watches.delete(id); }
      })();
    });
    const createWindow = () => {
      window = new BrowserWindow({ title: 'Pico', width: 1120, height: 800, minWidth: 720, minHeight: 520, backgroundColor: '#f7f7f3',
        webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, sandbox: true, nodeIntegration: false } });
      window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
      window.webContents.on('will-navigate', event => event.preventDefault());
      window.webContents.on('render-process-gone', stopWatches);
      window.webContents.on('did-start-loading', stopWatches);
      window.on('closed', () => { stopWatches(); window = null; workspaces.clear(); });
      void window.loadURL(PAGE);
    };
    createWindow(); app.on('activate', () => { if (!window) createWindow(); });
  });
  app.on('window-all-closed', () => app.quit());
}
