import { test, expect, _electron as electron } from '@playwright/test';
import { execFileSync, spawn } from 'node:child_process';
import { createServer } from 'node:http';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { portableApp } from './portable-app';


test('native workflow reopens to frozen dirty-worktree Diff, checks, tool links and current drift; narrow IME stays usable', async () => {
  test.setTimeout(120000);
  const portable = await portableApp();
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'pico-review-'));
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), 'pico-review-profile-'));
  const env = { PATH: '/usr/bin:/bin:/usr/sbin:/sbin', HOME: os.homedir(), TMPDIR: os.tmpdir(), PICO_CONFIG_DIR: path.join(profile, 'settings') };
  const python = path.join(portable.bundle, 'Contents/Resources/backend/python/bin/python3');
  const query = (method: string, params = {}) => JSON.parse(execFileSync(python, ['-I', '-B', '-c',
    'import json,sys;from pico.host_client import HostClient;print(json.dumps(HostClient(sys.argv[1]).call(sys.argv[2],json.loads(sys.argv[3]))))',
    root, method, JSON.stringify(params)], { env, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }));
  const git = (...args: string[]) => execFileSync('/usr/bin/git', args, { cwd: root, env, encoding: 'utf8' });
  const malicious = '<img src=x onerror="globalThis.__reviewAttack=1"><a href="javascript:window.pico.upgradeHost()">link</a>';
  const model = createServer((request, response) => {
    let raw = ''; request.on('data', c => { raw += c; });
    request.on('end', () => {
      const message = JSON.parse(raw).messages.at(-1);
      const calling = typeof message.content === 'string';
      const content = calling ? [
        { type: 'tool_use', id: 'review-patch', name: 'patch_file', input: { path: 'README.md', old_text: 'user\n', new_text: 'user\nagent\n' } },
        { type: 'tool_use', id: 'review-large', name: 'run_shell', input: { command: 'i=0; while [ $i -lt 3000 ]; do printf "new-%s\\n" "$i"; i=$((i+1)); done > large.txt' } },
        { type: 'tool_use', id: 'review-check', name: 'run_shell', input: { command: 'printf check-ok', purpose: 'verification' } },
      ] : [{ type: 'text', text: '任务完成。所有测试通过。' }];
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ content, stop_reason: calling ? 'tool_use' : 'end_turn' }));
    });
  });
  await new Promise<void>(resolve => model.listen(0, '127.0.0.1', resolve));
  await fs.writeFile(path.join(root, '.gitignore'), '.env\n.pico/\n');
  await fs.writeFile(path.join(root, '.env'), `PICO_PROVIDER=anthropic\nPICO_ANTHROPIC_API_BASE=http://127.0.0.1:${(model.address() as { port: number }).port}\nPICO_ANTHROPIC_MODEL=review-fixture\n`);
  await fs.writeFile(path.join(root, 'README.md'), 'head\n' + malicious + '\n');
  await fs.writeFile(path.join(root, 'large.txt'), Array.from({ length: 3000 }, (_, i) => `old-${i}\n`).join(''));
  git('init', '-q'); git('add', '.'); git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'initial');
  await fs.writeFile(path.join(root, 'README.md'), 'staged\n' + malicious + '\n'); git('add', 'README.md');
  await fs.writeFile(path.join(root, 'README.md'), 'user\n' + malicious + '\n');
  await fs.writeFile(path.join(root, 'notes.txt'), 'user-owned notes');
  const index = await fs.readFile(path.join(root, '.git/index'));
  let application: Awaited<ReturnType<typeof electron.launch>> | undefined;
  const launch = () => electron.launch({ executablePath: path.join(portable.bundle, 'Contents/MacOS/Pico'), args: [`--user-data-dir=${profile}`], cwd: root, env });
  try {
    application = await launch(); let window = await application.firstWindow();
    await expect(window.getByRole('heading', { name: '从一个工作区开始。' })).toBeVisible();
    await application.evaluate(({ dialog }, root) => { dialog.showOpenDialog = (async () => ({ canceled: false, filePaths: [root] })) as typeof dialog.showOpenDialog; }, root);
    await window.getByRole('button', { name: '选择文件夹 →' }).click();
    const prompt = window.getByLabel('发送任务', { exact: true });
    await prompt.fill('保留已有改动，添加实现并执行验证');
    await prompt.dispatchEvent('compositionstart');
    await prompt.dispatchEvent('keydown', { key: 'Enter', code: 'Enter', ctrlKey: true, isComposing: true, keyCode: 229 });
    expect(query('runs.list')).toHaveLength(0);
    await prompt.dispatchEvent('compositionend');
    await window.getByRole('button', { name: '发送任务', exact: true }).click();
    await expect(window.getByTestId('approval-card')).toContainText('patch_file');
    await window.getByRole('button', { name: '批准', exact: true }).click();
    await expect(window.getByTestId('approval-card')).toContainText('run_shell');
    const runId = query('runs.list')[0].run_id;
    await application.close(); application = undefined;
    expect(query('runs.get', { run_id: runId }).status).toBe('waiting_approval');
    application = await launch(); window = await application.firstWindow();
    await expect(window.getByTestId('run-id')).toHaveText(runId);
    await window.getByRole('button', { name: '批准', exact: true }).click();
    await expect.poll(() => query('approvals.list')[0]?.call_id).toBe('review-check');
    await expect(window.getByTestId('approval-card')).toContainText('verification');
    await window.getByRole('button', { name: '批准', exact: true }).click();
    await expect(window.getByTestId('run-status')).toHaveText('已完成');
    await window.getByRole('button', { name: '修改与验证', exact: true }).click();
    const panel = window.getByTestId('review-panel');
    await expect(panel.getByTestId('review-state')).toHaveText('冻结审查已就绪');
    await panel.getByLabel('文件范围').selectOption('initial');
    await expect(panel.getByText('这些修改在运行开始前已经存在，不计作本次运行的交付。')).toBeVisible();
    await expect(panel.getByLabel('差异文件')).toContainText('README.md');
    await expect(panel.getByLabel('差异文件')).toContainText('notes.txt');
    await panel.getByLabel('文件范围').selectOption('run');
    await panel.getByRole('button', { name: /^README.md/ }).click();
    const diff = panel.getByLabel('冻结 Diff 文本');
    await expect(diff).toContainText('+agent');
    await expect(diff).not.toContainText('staged');
    await expect(diff).not.toContainText('head');
    await expect(diff).toContainText(malicious);
    expect(await panel.locator('a').count()).toBe(0);
    expect(await window.evaluate(() => (globalThis as { __reviewAttack?: number }).__reviewAttack)).toBeUndefined();
    const frozen = await diff.textContent();
    await fs.writeFile(path.join(root, 'README.md'), 'external edit after run\n');
    await panel.getByRole('button', { name: '刷新当前漂移' }).click();
    await expect(panel.getByTestId('current-drift')).toContainText('观察范围已变化');
    await expect(panel.getByTestId('current-drift')).toContainText('验证版本已过期');
    await expect(panel.getByTestId('frozen-checks')).toContainText('已记录命令成功');
    expect(await diff.textContent()).toBe(frozen);
    await panel.getByRole('button', { name: /^large.txt/ }).click();
    await expect(diff).toContainText('old-0');
    const first = await diff.textContent();
    await panel.getByRole('button', { name: '下一段 Diff' }).click();
    await expect.poll(() => diff.textContent()).not.toBe(first);
    await panel.getByRole('button', { name: '上一段 Diff' }).click();
    await expect(diff).toHaveText(first!);
    await window.locator('.tool-records > summary').click();
    const tool = window.locator('[data-call-id="review-check"]');
    await tool.locator('summary').click();
    await tool.getByRole('button', { name: '查看检查记录' }).click();
    const check = panel.locator('[data-check-id="review-check"]');
    await expect(check).toHaveClass(/linked/);
    await expect(check).toContainText('exit 0'); await expect(check).toContainText('printf check-ok');
    await check.locator('summary').filter({ hasText: 'stdout' }).click();
    await expect(check).toContainText('check-ok');
    expect(await fs.readFile(path.join(root, '.git/index'))).toEqual(index);
    expect(await fs.readFile(path.join(root, 'notes.txt'), 'utf8')).toBe('user-owned notes');
    await window.screenshot({ path: 'test-results/review-wide.png' });
    await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setBounds({ width: 760, height: 620 }));
    await window.getByRole('button', { name: '修改与验证', exact: true }).click();
    await expect(panel).toBeVisible();
    await window.getByRole('button', { name: '对话与运行', exact: true }).click();
    await expect(window.getByTestId('run-panel')).toBeVisible();
    const fileTool = window.locator('[data-call-id="review-patch"]'); await fileTool.locator('summary').click();
    await fileTool.getByRole('button', { name: '查看文件 README.md', exact: true }).click();
    await expect(panel).toBeVisible(); await expect(panel.getByLabel('冻结 Diff 文本')).toContainText('+agent');
    expect(await window.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await window.screenshot({ path: 'test-results/review-narrow.png' });
    await window.getByRole('button', { name: '对话与运行', exact: true }).click();
    await window.getByLabel('发送任务', { exact: true }).fill('继续确认');
    await window.getByRole('button', { name: '发送任务', exact: true }).click();
    await expect.poll(() => query('runs.list').length).toBe(2);
    const second = query('runs.list')[0]; expect(second.session_id).toBe(query('runs.get', { run_id: runId }).session_id);
    query('runs.cancel', { run_id: second.run_id });
    await fs.writeFile('test-results/review-workflow.json', JSON.stringify({ native_protocol: true, dirty_index_preserved: true,
      frozen_diff_preserved: true, current_drift_separate: true, check_exit_zero: true, long_diff_paged: true,
      tool_file_check_links: true, narrow_layout: true, ime_did_not_submit: true, model_file_html_inert: true,
      closed_reopened_same_run: true, continued_same_session: true }, null, 2));
  } finally {
    await application?.close().catch(() => {});
    try { query('host.stop'); } catch { /* Already stopped. */ }
    await new Promise<void>(resolve => model.close(() => resolve()));
    await fs.rm(root, { recursive: true, force: true }); await fs.rm(profile, { recursive: true, force: true }); await portable.remove();
  }
});

test('review keeps failed, stale, not-run, interrupted and cancelled evidence honest; large activity stays bounded', async () => {
  test.setTimeout(120000);
  const portable = await portableApp();
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'pico-review-states-'));
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), 'pico-review-states-profile-'));
  const env = { PATH: '/usr/bin:/bin:/usr/sbin:/sbin', HOME: os.homedir(), TMPDIR: os.tmpdir(), PICO_CONFIG_DIR: path.join(profile, 'settings') };
  const python = path.join(portable.bundle, 'Contents/Resources/backend/python/bin/python3');
  const query = (method: string, params = {}) => JSON.parse(execFileSync(python, ['-I', '-B', '-c',
    'import json,sys;from pico.host_client import HostClient;print(json.dumps(HostClient(sys.argv[1]).call(sys.argv[2],json.loads(sys.argv[3]))))',
    root, method, JSON.stringify(params)], { env, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }));
  // Private fixture injection only: actual workers execute tools, collect snapshots and record results.
  const host = spawn(python, ['-I', '-B', '-c', `import asyncio,sys
from pico.host import Host
from pico.application import ApplicationService
from pico.providers.types import ModelResponse,ToolCall
original=ApplicationService.dispatch
def check(command,id='check'):
    return ToolCall('run_shell',{'command':command,'purpose':'verification'},id=id)
async def dispatch(self,method,params=None):
    if method=='fixture.kill_worker':
        self.manager.process.kill()
        return {}
    if method=='fixture.activity':
        with self.store.transaction():
            for i in range(1500):
                self.store._event(params['run_id'],'fixture_activity',{'index':i})
        return {}
    if method=='runs.start':
        kind=params['prompt']
        write=ToolCall('write_file',{'path':kind+'.txt','content':'known change'},id='write-'+kind)
        calls=[write]
        if kind=='failed':
            calls += [check("i=0; while [ $i -lt 20000 ]; do printf x; i=$((i+1)); done; exit 1")]
        elif kind=='stale':
            calls=[check('printf ok'),write]
        elif kind=='paged':
            calls=[ToolCall('run_shell',{'command':'i=0; while [ $i -lt 60 ]; do printf changed > file-$i.txt; i=$((i+1)); done'},id='many-files')]
            calls += [check('printf check-'+str(i),'check-'+str(i)) for i in range(27)]
        elif kind in ('unknown','cancelled'):
            calls += [check('printf ready > .pico/'+kind+'-started; sleep 20')]
        self.test_script=[ModelResponse(tool_calls=calls),ModelResponse(text='所有测试通过！')]
    return await original(self,method,params)
ApplicationService.dispatch=dispatch
asyncio.run(Host(sys.argv[1],test_script=[]).serve())`, root], { env, stdio: 'ignore' });
  let application: Awaited<ReturnType<typeof electron.launch>> | undefined;
  try {
    await expect.poll(() => { try { return query('host.info').status; } catch { return ''; } }).toBe('ready');
    const session = query('sessions.create', { config: { provider: 'ollama', approval: 'auto' } });
    application = await electron.launch({ executablePath: path.join(portable.bundle, 'Contents/MacOS/Pico'), args: [`--user-data-dir=${profile}`], env });
    const window = await application.firstWindow();
    await expect(window.getByRole('heading', { name: '从一个工作区开始。' })).toBeVisible();
    await application.evaluate(({ dialog }, root) => { dialog.showOpenDialog = (async () => ({ canceled: false, filePaths: [root] })) as typeof dialog.showOpenDialog; }, root);
    await window.getByRole('button', { name: '选择文件夹 →' }).click();
    await window.getByRole('button', { name: '修改与验证', exact: true }).click();
    const panel = window.getByTestId('review-panel');
    const start = async (kind: string, done = true) => {
      const run = query('runs.start', { session_id: session.id, prompt: kind, client_request_id: kind });
      if (done) await expect.poll(() => query('runs.get', { run_id: run.run_id }).status, { timeout: 20000 }).toBe('completed');
      await expect(window.getByLabel('选择运行').locator(`option[value="${run.run_id}"]`)).toHaveCount(1);
      await window.getByLabel('选择运行').selectOption(run.run_id);
      await expect(window.getByTestId('run-id')).toHaveText(run.run_id);
      return run.run_id as string;
    };
    await start('failed');
    await expect(panel.getByTestId('frozen-checks')).toContainText('命令失败');
    await panel.getByRole('tab', { name: '验证记录' }).click();
    await expect(panel.getByRole('heading', { name: '命令失败', exact: true })).toBeVisible();
    await expect(panel).toContainText('exit 1');
    await panel.getByText('stdout（已截断） / stderr', { exact: true }).click();
    const output = panel.locator('[data-check-id="check"] details').last().locator('pre').first();
    expect((await output.textContent())!.length).toBeLessThanOrEqual(8192);
    await start('not-run');
    await expect(panel.getByTestId('frozen-checks')).toContainText('未记录验证命令');
    await panel.getByRole('tab', { name: '验证记录' }).click();
    await expect(panel.getByTestId('checks-empty')).toBeVisible();
    await start('stale');
    await expect(panel.getByTestId('frozen-checks')).toContainText('验证版本已过期');
    await start('paged');
    await expect(panel.getByLabel('差异文件').locator('li')).toHaveCount(25);
    const first = await panel.getByLabel('差异文件').textContent();
    await panel.getByRole('button', { name: '下一页文件' }).click();
    await expect.poll(() => panel.getByLabel('差异文件').textContent()).not.toBe(first);
    await panel.getByRole('button', { name: '上一页文件' }).click();
    await expect(panel.getByLabel('差异文件')).toHaveText(first!);
    await panel.getByRole('tab', { name: '验证记录' }).click();
    await expect(panel.locator('[data-check-id]')).toHaveCount(25);
    await panel.getByRole('button', { name: '下一页检查' }).click();
    await expect(panel.locator('[data-check-id]')).toHaveCount(2);
    await panel.getByRole('button', { name: '上一页检查' }).click();
    await expect(panel.locator('[data-check-id]')).toHaveCount(25);
    const interrupted = await start('unknown', false);
    await expect.poll(() => fs.readFile(path.join(root, '.pico/unknown-started'), 'utf8').catch(() => '')).toBe('ready');
    query('fixture.kill_worker');
    await expect(window.getByTestId('run-status')).toHaveText('运行中断');
    await expect(panel.getByTestId('frozen-checks')).toContainText('结果未知');
    await expect(panel.getByLabel('差异文件')).toContainText('unknown.txt');
    await expect(panel.getByTestId('diff-unavailable')).toContainText('end_snapshot_missing');
    await panel.getByRole('tab', { name: '验证记录' }).click();
    await expect(panel.locator('[data-check-id="check"]')).toContainText('exit 未知');
    const cancelled = await start('cancelled', false);
    await expect.poll(() => fs.readFile(path.join(root, '.pico/cancelled-started'), 'utf8').catch(() => '')).toBe('ready');
    query('fixture.activity', { run_id: cancelled });
    await panel.getByRole('button', { name: '刷新当前漂移' }).click();
    await expect(panel.getByRole('status').filter({ hasText: '有活动任务' })).toBeVisible();
    const before = Date.now();
    await window.getByRole('button', { name: '取消运行', exact: true }).click();
    await expect(window.getByTestId('run-status')).toHaveText('已取消', { timeout: 10000 });
    const cancelMs = Date.now() - before;
    await expect(panel.getByTestId('frozen-checks')).toContainText('检查已取消');
    await expect(panel.getByLabel('冻结 Diff 文本')).toContainText('+known change');
    const handle = await window.evaluate(async () => (await window.pico.restoreWorkspace())!.handle);
    for (const [method, params] of [['runs.diff', { run_id: cancelled, file_id: 'x', path: '/etc/passwd' }], ['fixture.kill_worker', {}], ['review_blob', {}]] as const) {
      expect(await window.evaluate(async ({ handle, method, params }) => {
        try { await window.pico.request(handle, method as never, params as never); return false; } catch { return true; }
      }, { handle, method, params })).toBe(true);
    }
    await window.screenshot({ path: 'test-results/review-states.png' });
    await fs.writeFile('test-results/review-states.json', JSON.stringify({ real_worker_tools: true, failed_exit: 1,
      not_run_not_passed: true, stale_after_write: true, unknown_worker: interrupted, cancelled_run: cancelled,
      unknown_diff_reason: 'end_snapshot_missing', files_per_page: 25, checks_pages: [25, 2],
      stdout_display_limit: 8192, synthetic_activity_events: 1500, cancel_response_ms: cancelMs, restricted_bridge: true }, null, 2));
  } finally {
    await application?.close().catch(() => {});
    try { query('host.stop'); } catch { /* Already stopped. */ }
    if (host.exitCode === null && host.signalCode === null) await new Promise<void>(resolve => host.once('exit', () => resolve()));
    await fs.rm(root, { recursive: true, force: true }); await fs.rm(profile, { recursive: true, force: true }); await portable.remove();
  }
});
