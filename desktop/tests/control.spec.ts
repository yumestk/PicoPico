import { test, expect, _electron as electron } from '@playwright/test';
import { execFileSync, spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createServer } from 'node:http';
import { portableApp } from './portable-app';

// Real packaged renderer/main/preload, real Host/worker/guardian and a local native HTTP provider.
test('desktop and CLI share approval/cancel and recover the same submission after a lost ACK and app exits', async () => {
  test.setTimeout(120000);
  const portable = await portableApp();
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'pico-controls-'));
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), 'pico-controls-profile-'));
  const python = path.join(portable.bundle, 'Contents/Resources/backend/python/bin/python3');
  const env = { PATH: '/usr/bin:/bin:/usr/sbin:/sbin', HOME: os.homedir(), TMPDIR: os.tmpdir() };
  const query = (method: string, params = {}) => JSON.parse(execFileSync(python, ['-I', '-B', '-c',
    'import json,sys;from pico.host_client import HostClient;print(json.dumps(HostClient(sys.argv[1]).call(sys.argv[2],json.loads(sys.argv[3]))))',
    root, method, JSON.stringify(params)], { env, encoding: 'utf8' }));
  const cli = (...args: string[]) => execFileSync(python, ['-I', '-B', '-m', 'pico', '--cwd', root, ...args], { env, encoding: 'utf8' }).trim();
  const malicious = '<img src=x onerror="globalThis.__attack=1">';
  let nativeRequests = 0;
  const model = createServer((request, response) => {
    let body = ''; request.on('data', c => { body += c; });
    request.on('end', () => {
      nativeRequests++;
      const message = JSON.parse(body).messages.at(-1);
      const calling = typeof message.content === 'string';
      const command = calling && message.content.includes('long wait')
        ? 'printf ready > .pico/long-started; sleep 20' : calling && message.content.includes('deny write')
          ? 'printf forbidden > denied-proof' : 'printf desktop > desktop-proof';
      const content = calling ? [{ type: 'tool_use', id: `call-${nativeRequests}`, name: 'run_shell', input: { command } }]
        : [{ type: 'text', text: `原生任务已完成。${malicious}` }];
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ content, stop_reason: calling ? 'tool_use' : 'end_turn' }));
    });
  });
  await new Promise<void>(resolve => model.listen(0, '127.0.0.1', resolve));
  await fs.writeFile(path.join(root, '.env'), `PICO_PROVIDER=anthropic\nPICO_ANTHROPIC_API_BASE=http://127.0.0.1:${(model.address() as { port: number }).port}\nPICO_ANTHROPIC_MODEL=desktop-fixture\n`);
  const host = spawn(python, ['-I', '-B', '-c', `import asyncio,sys
from pico.host import Host
from pico.application import ApplicationService
original=ApplicationService.dispatch
first=[True]
async def dispatch(self,method,params=None):
    result=await original(self,method,params)
    if method=='runs.start' and first[0]:
        first[0]=False
        await asyncio.sleep(6.5)
    return result
ApplicationService.dispatch=dispatch
asyncio.run(Host(sys.argv[1]).serve())`, root], { env, stdio: 'ignore' });
  let application: Awaited<ReturnType<typeof electron.launch>> | undefined;
  const launch = () => electron.launch({ executablePath: path.join(portable.bundle, 'Contents/MacOS/Pico'),
    args: [`--user-data-dir=${profile}`], cwd: root, env });
  try {
    await expect.poll(() => { try { return query('host.info').api_version; } catch { return 0; } }).toBe(4);
    application = await launch(); let window = await application.firstWindow();
    await expect(window.getByRole('heading', { name: '从一个工作区开始。' })).toBeVisible();
    await application.evaluate(({ dialog }, root) => { dialog.showOpenDialog = (async () => ({ canceled: false, filePaths: [root] })) as typeof dialog.showOpenDialog; }, root);
    await window.getByRole('button', { name: '选择文件夹 →' }).click();
    await window.getByRole('button', { name: '新建会话', exact: true }).click();
    await expect(window.getByLabel('发送任务', { exact: true })).toBeVisible();
    await window.getByLabel('发送任务', { exact: true }).fill('desktop check');
    // Two synchronous submissions cannot mint two intents before React rerenders.
    await window.locator('form').evaluate((form: HTMLFormElement) => { form.requestSubmit(); form.requestSubmit(); });
    await expect(window.getByRole('button', { name: '重试确认同一次发送' })).toBeVisible({ timeout: 12000 });
    const session = query('sessions.list')[0];
    const runs = query('runs.list', { cursor: '', session_id: session.id }).items;
    expect(runs).toHaveLength(1);
    const original = runs[0].run_id;
    await expect(window.getByTestId('approval-card')).toBeVisible();
    // A native window close exits the app; the waiting task and durable outbox survive.
    const firstProcess = application.process();
    await window.close();
    await expect.poll(() => firstProcess.exitCode).not.toBeNull(); application = undefined;
    expect(query('runs.get', { run_id: original }).status).toBe('waiting_approval');
    application = await launch(); window = await application.firstWindow();
    await expect(window.getByTestId('workspace-path')).toHaveText(await fs.realpath(root));
    await window.getByRole('button', { name: '重试确认同一次发送' }).click();
    await expect(window.getByTestId('run-id')).toHaveText(original);
    expect(query('runs.list', { cursor: '', session_id: session.id }).items).toHaveLength(1);
    const approval = query('approvals.list')[0];
    expect(JSON.parse(cli('approvals', 'approve', approval.id)).decision).toBe(1);
    await expect(window.getByTestId('run-status')).toHaveText('已完成');
    await expect(window.getByText(`原生任务已完成。${malicious}`, { exact: true })).toHaveCount(1);
    expect(await fs.readFile(path.join(root, 'desktop-proof'), 'utf8')).toBe('desktop');
    expect(await window.evaluate(() => (globalThis as { __attack?: number }).__attack)).toBeUndefined();
    expect(window.url()).toBe('pico://app/index.html');
    const running = cli('--session', session.id, '--approval', 'auto', 'run', 'cli long wait', '--detach');
    await expect.poll(() => fs.readFile(path.join(root, '.pico/long-started'), 'utf8').catch(() => '')).toBe('ready');
    await expect(window.getByLabel('选择运行').locator(`option[value="${running}"]`)).toHaveCount(1);
    await window.getByLabel('选择运行').selectOption(running);
    await expect(window.getByTestId('run-id')).toHaveText(running);
    // Explicit application quit also detaches from an already executing shell.
    await application.close(); application = undefined;
    expect(query('runs.get', { run_id: running }).status).toBe('running');
    application = await launch(); window = await application.firstWindow();
    await expect(window.getByTestId('run-id')).toHaveText(running);
    await window.getByRole('button', { name: '取消运行', exact: true }).click();
    await expect(window.getByTestId('run-status')).toHaveText('已取消');
    expect(JSON.parse(cli('runs', 'get', running)).status).toBe('cancelled');
    await window.getByLabel('发送任务', { exact: true }).fill('deny write');
    await window.getByRole('button', { name: '发送任务', exact: true }).click();
    await expect(window.getByTestId('approval-card')).toBeVisible();
    await window.getByRole('button', { name: '拒绝', exact: true }).click();
    await expect(window.getByTestId('approval-card')).toHaveCount(0);
    await expect(window.getByTestId('run-status')).toHaveText('已完成');
    expect(await fs.stat(path.join(root, 'denied-proof')).then(() => true, () => false)).toBe(false);
    await window.screenshot({ path: 'test-results/controls.png' });
    await fs.writeFile('test-results/controls.json', JSON.stringify({ nativeRequests, desktop_submit_cli_approve: true,
      cli_submit_desktop_cancel: true, lost_ack_same_run: original, duplicate_submit_count: runs.length,
      close_and_quit_recovered: true, literal_model_html: true, approval_denied_without_effect: true, packaged: true }, null, 2));
  } finally {
    await application?.close().catch(() => {});
    try { query('host.stop'); } catch { /* Already stopped. */ }
    await new Promise<void>(resolve => { if (host.exitCode !== null) resolve(); else host.once('exit', () => resolve()); });
    await new Promise<void>(resolve => model.close(() => resolve()));
    await fs.rm(root, { recursive: true, force: true }); await fs.rm(profile, { recursive: true, force: true }); await portable.remove();
  }
});

test('workspace switches reject late history and blocked work is not submitted; paged history survives reload', async () => {
  test.setTimeout(90000);
  const portable = await portableApp();
  const base = await fs.mkdtemp(path.join(os.tmpdir(), 'pico-switch-'));
  const profile = path.join(base, 'profile'); await fs.mkdir(profile);
  const roots = [path.join(base, 'workspace-a'), path.join(base, 'workspace-b')];
  const python = path.join(portable.bundle, 'Contents/Resources/backend/python/bin/python3');
  const env = { PATH: '/usr/bin:/bin', HOME: os.homedir(), TMPDIR: os.tmpdir() };
  const hosts = [];
  const query = (root: string, method: string, params = {}) => JSON.parse(execFileSync(python, ['-I', '-B', '-c',
    'import json,sys;from pico.host_client import HostClient;print(json.dumps(HostClient(sys.argv[1]).call(sys.argv[2],json.loads(sys.argv[3]))))',
    root, method, JSON.stringify(params)], { env, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }));
  for (const [index, root] of roots.entries()) {
    await fs.mkdir(root);
    hosts.push(spawn(python, ['-I', '-B', '-c', `import asyncio,sys
from pathlib import Path
from pico.host import Host
from pico.application import ApplicationService
original_open=ApplicationService.open
original_dispatch=ApplicationService.dispatch
async def opening(self):
    await original_open(self)
    session=await original_dispatch(self,'sessions.create',{'config':{'provider':'ollama'}})
    private=self.store.load_session(session['id'])
    private['history']=[{'role':'assistant','content':sys.argv[2]+'-'+str(i).zfill(3)} for i in range(80)]
    self.store.save_session(private)
    return self
async def dispatch(self,method,params=None):
    if method=='sessions.get' and sys.argv[2]=='ROOT-A':
        Path(sys.argv[1],'.pico/history-requested').touch()
        await asyncio.sleep(2.5)
    return await original_dispatch(self,method,params)
ApplicationService.open=opening
ApplicationService.dispatch=dispatch
asyncio.run(Host(sys.argv[1],test_script=[]).serve())`, root, index ? 'ROOT-B' : 'ROOT-A'], { env, stdio: 'ignore' }));
    await expect.poll(() => { try { return query(root, 'host.info').status; } catch { return ''; } }).toBe('ready');
  }
  const lease = spawn(python, ['-I', '-B', '-c', `import fcntl,sys,time
from pathlib import Path
with Path(sys.argv[1],'.pico/execution.lock').open('a+') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX)
    Path(sys.argv[1],'.pico/lease-held').touch()
    time.sleep(80)`, roots[1]], { env, stdio: 'ignore' });
  let application: Awaited<ReturnType<typeof electron.launch>> | undefined;
  try {
    await expect.poll(() => fs.stat(path.join(roots[1], '.pico/lease-held')).then(() => true, () => false)).toBe(true);
    application = await electron.launch({ executablePath: path.join(portable.bundle, 'Contents/MacOS/Pico'), args: [`--user-data-dir=${profile}`], env });
    const window = await application.firstWindow();
    await expect(window.getByRole('heading', { name: '从一个工作区开始。' })).toBeVisible();
    const choose = async (root: string, first = false) => {
      await application!.evaluate(({ dialog }, root) => { dialog.showOpenDialog = (async () => ({ canceled: false, filePaths: [root] })) as typeof dialog.showOpenDialog; }, root);
      await window.getByRole('button', { name: first ? '选择文件夹 →' : '＋ 选择工作区', exact: true }).click();
      await expect(window.getByTestId('workspace-path')).toHaveText(await fs.realpath(root));
    };
    await choose(roots[0], true);
    await expect.poll(() => fs.stat(path.join(roots[0], '.pico/history-requested')).then(() => true, () => false)).toBe(true);
    await choose(roots[1]);
    await expect(window.getByText('ROOT-B-000', { exact: true })).toBeVisible();
    await expect(window.getByText('工作区已阻塞', { exact: true })).toBeVisible();
    await window.getByRole('button', { name: '继续加载历史' }).click();
    await expect(window.getByText('ROOT-B-059', { exact: true })).toBeAttached();
    await window.getByRole('button', { name: '继续加载历史' }).click();
    await expect(window.getByText('ROOT-B-079', { exact: true })).toBeAttached();
    await expect(window.locator('[data-message-id]')).toHaveCount(80);
    await window.getByLabel('发送任务', { exact: true }).fill('blocked request');
    await window.getByRole('button', { name: '发送任务', exact: true }).click();
    await expect(window.getByRole('alert').filter({ hasText: 'workspace_blocked' })).toBeVisible();
    expect(query(roots[1], 'runs.list')).toHaveLength(0);
    await expect(window.getByRole('button', { name: '重试确认同一次发送' })).toHaveCount(0);
    // Wait for the first workspace's delayed responses, then verify the new view remains isolated.
    await new Promise(resolve => setTimeout(resolve, 2800));
    await expect(window.getByText(/^ROOT-A-/)).toHaveCount(0);
    const handle = await window.evaluate(async () => (await window.pico.restoreWorkspace())!.handle);
    for (const [method, params] of [['host.stop', {}], ['permit', {}], ['runs.get', { run_id: 'x', socket: '/tmp/escape' }],
      ['sessions.create', { config: { api_key: 'not-allowed' } }]] as const) {
      const denied = await window.evaluate(async ({ handle, method, params }) => {
        try { await window.pico.request(handle, method as never, params as never); return false; } catch { return true; }
      }, { handle, method, params });
      expect(denied).toBe(true);
    }
    lease.kill('SIGTERM');
    await expect.poll(() => query(roots[1], 'host.info').status).toBe('ready');
    await window.getByRole('button', { name: '刷新连接', exact: true }).click();
    await expect(window.getByText('已连接', { exact: true })).toBeVisible();
    await expect(window.getByText('ROOT-B-000', { exact: true })).toBeVisible();
    // Renderer reload rebuilds authoritative state, not a second persisted run store.
    await window.reload();
    await expect(window.getByTestId('workspace-path')).toHaveText(await fs.realpath(roots[1]));
    await expect(window.getByText('ROOT-B-000', { exact: true })).toBeVisible();
    await expect(window.locator('[data-message-id]')).toHaveCount(30);
    await fs.writeFile(path.join(roots[1], '.env'), 'PICO_PROVIDER=invalid-private-config-value\n');
    await window.getByRole('button', { name: '刷新连接', exact: true }).click();
    await expect(window.getByRole('alert').filter({ hasText: 'configuration_error' })).toBeVisible();
    await expect(window.getByText('连接已中断，正在重连。', { exact: false })).toHaveCount(0);
    await fs.writeFile(path.join(roots[1], '.env'), 'PICO_PROVIDER=ollama\n');
    await window.getByRole('button', { name: '刷新连接', exact: true }).click();
    await expect(window.getByText('ROOT-B-000', { exact: true })).toBeVisible();
    const previousHost = query(roots[1], 'host.info').instance_id;
    hosts[1].kill('SIGKILL');
    await expect(window.getByRole('status').filter({ hasText: '连接已中断' })).toBeVisible({ timeout: 10000 });
    await expect(window.getByRole('status').filter({ hasText: '连接已中断' })).toHaveCount(0, { timeout: 20000 });
    expect(query(roots[1], 'host.info').instance_id).not.toBe(previousHost);
    await expect(window.getByText('ROOT-B-000', { exact: true })).toBeVisible();
    await fs.writeFile('test-results/switching.json', JSON.stringify({ delayed_workspace_isolated: true, history_pages: 80,
      blocked_submission_count: 0, forbidden_bridge_methods_rejected: true, renderer_reload_restored: true,
      config_error_recovered: true, disconnected_host_recovered: true }, null, 2));
  } finally {
    await application?.close().catch(() => {});
    if (lease.exitCode === null) lease.kill('SIGTERM');
    for (const [index, host] of hosts.entries()) {
      try { query(roots[index], 'host.stop'); } catch { /* Already stopped. */ }
      await new Promise<void>(resolve => { if (host.exitCode !== null || host.signalCode !== null) resolve(); else host.once('exit', () => resolve()); });
    }
    await fs.rm(base, { recursive: true, force: true }); await portable.remove();
  }
});

test('incompatible Host is visible and manual upgrade leaves its active approval untouched', async () => {
  test.setTimeout(60000);
  const portable = await portableApp();
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'pico-upgrade-'));
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), 'pico-upgrade-profile-'));
  const python = path.join(portable.bundle, 'Contents/Resources/backend/python/bin/python3');
  const env = { PATH: '/usr/bin:/bin', HOME: os.homedir(), TMPDIR: os.tmpdir() };
  const query = (method: string, params = {}) => JSON.parse(execFileSync(python, ['-I', '-B', '-c',
    'import json,sys;from pico.host_client import HostClient;print(json.dumps(HostClient(sys.argv[1]).call(sys.argv[2],json.loads(sys.argv[3]))))',
    root, method, JSON.stringify(params)], { env, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }));
  const host = spawn(python, ['-I', '-B', '-c', `import asyncio,sys
from pico.host import Host
from pico.application import ApplicationService
from pico.providers.types import ModelResponse,ToolCall
original=ApplicationService.dispatch
async def dispatch(self,method,params=None):
    result=await original(self,method,params)
    if method=='host.info':
        return {**result,'api_version':3,'capabilities':[c for c in result['capabilities'] if c not in ('desktop_control','idle_upgrade')]}
    return result
ApplicationService.dispatch=dispatch
asyncio.run(Host(sys.argv[1],test_script=[ModelResponse(tool_calls=[ToolCall('write_file',{'path':'protected','content':'must not execute'})])]).serve())`, root], { env, stdio: 'ignore' });
  let application: Awaited<ReturnType<typeof electron.launch>> | undefined;
  try {
    await expect.poll(() => { try { return query('host.info').api_version; } catch { return 0; } }).toBe(3);
    const session = query('sessions.create', { config: { provider: 'ollama', approval: 'ask' } });
    const run = query('runs.start', { session_id: session.id, prompt: 'wait for approval', client_request_id: 'old-run' });
    await expect.poll(() => query('approvals.list').length).toBe(1);
    application = await electron.launch({ executablePath: path.join(portable.bundle, 'Contents/MacOS/Pico'), args: [`--user-data-dir=${profile}`], env });
    const window = await application.firstWindow();
    await expect(window.getByRole('heading', { name: '从一个工作区开始。' })).toBeVisible();
    await application.evaluate(({ dialog }, root) => { dialog.showOpenDialog = (async () => ({ canceled: false, filePaths: [root] })) as typeof dialog.showOpenDialog; }, root);
    await window.getByRole('button', { name: '选择文件夹 →' }).click();
    await expect(window.getByRole('heading', { name: '需要升级工作区 Host' })).toBeVisible();
    await window.getByRole('button', { name: '升级空闲 Host' }).click();
    await expect(window.getByRole('alert')).toContainText('manual_upgrade_required');
    expect(query('runs.get', { run_id: run.run_id }).status).toBe('waiting_approval');
    expect(query('approvals.list')).toHaveLength(1);
    expect(await fs.stat(path.join(root, 'protected')).then(() => true, () => false)).toBe(false);
    // The explicit manual path finishes/cancels work first, then reconnects a new Host.
    query('runs.cancel', { run_id: run.run_id });
    await expect.poll(() => query('runs.get', { run_id: run.run_id }).status).toBe('cancelled');
    query('host.stop');
    await new Promise<void>(resolve => { if (host.exitCode !== null) resolve(); else host.once('exit', () => resolve()); });
    await window.getByRole('button', { name: '刷新连接', exact: true }).click();
    await expect(window.getByRole('heading', { name: '需要升级工作区 Host' })).toHaveCount(0);
    await expect(window.getByTestId('run-status')).toHaveText('已取消');
    expect(query('host.info').api_version).toBe(4);
    await fs.writeFile('test-results/upgrade.json', JSON.stringify({ incompatible_host_visible: true,
      active_task_untouched: true, manual_idle_upgrade_reconnected: true, sessions_preserved: true }, null, 2));
  } finally {
    await application?.close().catch(() => {});
    try { query('host.stop'); } catch { /* Already stopped. */ }
    if (host.exitCode === null) host.kill('SIGTERM');
    await fs.rm(root, { recursive: true, force: true }); await fs.rm(profile, { recursive: true, force: true }); await portable.remove();
  }
});
