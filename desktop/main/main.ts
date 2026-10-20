import { app, BrowserWindow, dialog, ipcMain, protocol, session } from 'electron';
import { execFile } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';
import { hostInfo } from './host-client';
import type { Workspace } from '../shared';

protocol.registerSchemesAsPrivileged([{ scheme: 'pico', privileges: { standard: true, secure: true, supportFetchAPI: true } }]);
const execute = promisify(execFile);
const workspaces = new Map<string, { workspace: string; socket: string }>();
let window: BrowserWindow | null;
const PAGE = 'pico://app/index.html';

function checkSender(event: Electron.IpcMainInvokeEvent) {
  if (!window || event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame
    || event.senderFrame.url !== PAGE) throw new Error('Untrusted desktop sender');
}

async function openWorkspace(directory: string): Promise<Workspace> {
  const backend = app.isPackaged ? path.join(process.resourcesPath, 'backend') : path.join(app.getAppPath(), 'runtime/backend');
  const python = path.join(backend, 'python/bin/python3');
  const { stdout } = await execute(python, ['-I', '-B', '-m', 'pico.desktop_bootstrap', '--workspace', directory], {
    cwd: backend, timeout: 20000, maxBuffer: 65536,
    env: { ...process.env, SSL_CERT_FILE: path.join(backend, 'cacert.pem') },
  });
  const value = JSON.parse(stdout);
  if (typeof value.workspace !== 'string' || typeof value.socket !== 'string'
    || !path.isAbsolute(value.workspace) || !path.isAbsolute(value.socket)) throw new Error('Invalid workspace bootstrap');
  const info = await hostInfo(value.socket, value.workspace);
  const handle = randomUUID();
  workspaces.set(handle, { workspace: value.workspace, socket: value.socket });
  return { handle, workspace: value.workspace, info };
}

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => { window?.show(); window?.focus(); });
  app.whenReady().then(() => {
    protocol.handle('pico', async request => {
      const url = new URL(request.url);
      const name = decodeURIComponent(url.pathname).slice(1);
      if (url.host !== 'app' || !(name === 'index.html' || /^assets\/[A-Za-z0-9_.-]+$/.test(name))) {
        return new Response('Not found', { status: 404 });
      }
      const content = await readFile(path.join(__dirname, '../renderer', name));
      const types: Record<string, string> = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' };
      return new Response(content, { headers: { 'Content-Type': types[path.extname(name)] ?? 'application/octet-stream' } });
    });
    session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    session.defaultSession.webRequest.onBeforeRequest((details, callback) => {
      callback({ cancel: !details.url.startsWith('pico://app/') });
    });
    ipcMain.handle('workspace:choose', async event => {
      checkSender(event);
      const result = await dialog.showOpenDialog(window!, { title: '选择 Pico 工作区', properties: ['openDirectory'] });
      return result.canceled ? null : openWorkspace(result.filePaths[0]);
    });
    ipcMain.handle('workspace:refresh', async (event, handle: unknown) => {
      checkSender(event);
      const selected = typeof handle === 'string' ? workspaces.get(handle) : undefined;
      if (!selected) throw new Error('Unknown workspace');
      return { handle, workspace: selected.workspace, info: await hostInfo(selected.socket, selected.workspace) };
    });
    const createWindow = () => {
      window = new BrowserWindow({
        title: 'Pico', width: 1040, height: 720, minWidth: 720, minHeight: 520,
        backgroundColor: '#f7f7f3',
        webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, sandbox: true, nodeIntegration: false },
      });
      window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
      window.webContents.on('will-navigate', event => event.preventDefault());
      window.on('closed', () => { window = null; workspaces.clear(); });
      void window.loadURL(PAGE);
    };
    createWindow();
    app.on('activate', () => { if (!window) createWindow(); });
  });
  // A desktop exit detaches; workspace Hosts are shared with other clients.
  app.on('window-all-closed', () => app.quit());
}
