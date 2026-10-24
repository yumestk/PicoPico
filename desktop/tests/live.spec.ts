import { test, expect, _electron as electron } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { portableApp } from './portable-app';

// Opt-in only: this test sends real requests and may incur provider charges.
test('live DeepSeek desktop approval, reopen, frozen review and cancellation', async () => {
  test.skip(process.env.PICO_LIVE_PROVIDER !== 'deepseek', 'Set PICO_LIVE_PROVIDER=deepseek to run paid live requests');
  test.setTimeout(240000);
  const portable = await portableApp();
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'pico-live-desktop-'));
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), 'pico-live-profile-'));
  const env = { PATH: '/usr/bin:/bin:/usr/sbin:/sbin', HOME: os.homedir(), TMPDIR: os.tmpdir(), PICO_CONFIG_DIR: path.join(profile, 'settings') };
  const python = path.join(portable.bundle, 'Contents/Resources/backend/python/bin/python3');
  const query = (method: string, params = {}) => JSON.parse(execFileSync(python, ['-I', '-B', '-c',
    'import json,sys;from pico.host_client import HostClient;print(json.dumps(HostClient(sys.argv[1]).call(sys.argv[2],json.loads(sys.argv[3]))))',
    root, method, JSON.stringify(params)], { env, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }));
  const git = (...args: string[]) => execFileSync('/usr/bin/git', args, { cwd: root, env, encoding: 'utf8' });
  let application: Awaited<ReturnType<typeof electron.launch>> | undefined;
  let runId: string | undefined;
  const launch = () => electron.launch({ executablePath: path.join(portable.bundle, 'Contents/MacOS/Pico'), args: [`--user-data-dir=${profile}`], cwd: root, env });
  try {
    // Import once into Pico, then exercise a project containing no credentials.
    const imported = JSON.parse(execFileSync(python, ['-I', '-B', '-m', 'pico.settings', 'import', '--file',
      path.join(path.resolve(process.env.PICO_LIVE_ENV ?? '..'), '.env')], { env, encoding: 'utf8' }));
    expect(imported.profiles.deepseek.api_key_configured).toBe(true);
    await fs.writeFile(path.join(root, '.gitignore'), '.env\n.pico/\n');
    await fs.writeFile(path.join(root, 'sample.txt'), 'user header\nvalue = "beta"\n');
    await fs.writeFile(path.join(root, 'verify.sh'), '#!/bin/sh\ngrep -Fx \'value = "beta-native"\' sample.txt\n');
    git('init', '-q'); git('add', '.'); git('-c', 'user.name=Live smoke', '-c', 'user.email=smoke@example.invalid', 'commit', '-qm', 'initial');
    await fs.writeFile(path.join(root, 'sample.txt'), 'user header\nvalue = "beta"\nkeep existing edit\n');
    const index = await fs.readFile(path.join(root, '.git/index'));
    application = await launch(); let window = await application.firstWindow();
    await expect(window.getByRole('heading', { name: '从一个工作区开始。' })).toBeVisible();
    await application.evaluate(({ dialog }, root) => { dialog.showOpenDialog = (async () => ({ canceled: false, filePaths: [root] })) as typeof dialog.showOpenDialog; }, root);
    await window.getByRole('button', { name: '选择文件夹 →' }).click();
    await expect(window.getByTestId('workspace-path')).toHaveText(await fs.realpath(root));
    // Bound network latency and tool count without changing the application's production defaults.
    const created = query('sessions.create', { config: { provider: 'deepseek', approval: 'ask', max_steps: 8,
      max_new_tokens: 1024, openai_timeout: 45, allowed_tools: ['read_file', 'patch_file', 'run_shell'] } });
    const sessionId = created.id as string;
    await expect(window.getByRole('button').filter({ hasText: sessionId.slice(-8) })).toBeVisible();
    await window.getByRole('button').filter({ hasText: sessionId.slice(-8) }).click();
    const configuration = query('configuration.get', { session_id: sessionId });
    expect(configuration.provider).toBe('deepseek'); expect(configuration.api_key_configured).toBe(true);
    await window.getByLabel('发送任务', { exact: true }).fill(
      'Read sample.txt with read_file. Use patch_file exactly once to replace only value = "beta" with value = "beta-native"; preserve all other lines. ' +
      'Read the updated file. Then use run_shell exactly once with command "sh verify.sh" and purpose "verification". Stop after that and report its real exit code. Do not modify other files.');
    await window.getByRole('button', { name: '发送任务', exact: true }).click();
    await expect(window.getByTestId('approval-card')).toContainText('patch_file', { timeout: 70000 });
    runId = query('runs.list')[0].run_id;
    await window.getByRole('button', { name: '批准', exact: true }).click();
    await expect(window.getByTestId('approval-card')).toContainText('run_shell', { timeout: 70000 });
    const approval = query('approvals.list').find((a: { run_id: string }) => a.run_id === runId);
    expect(approval.args).toMatchObject({ command: 'sh verify.sh', purpose: 'verification' });
    await application.close(); application = undefined;
    expect(query('runs.get', { run_id: runId }).status).toBe('waiting_approval');
    application = await launch(); window = await application.firstWindow();
    await expect(window.getByTestId('run-id')).toHaveText(runId!);
    await window.getByRole('button', { name: '批准', exact: true }).click();
    await expect(window.getByTestId('run-status')).toHaveText('已完成', { timeout: 70000 });
    expect(await fs.readFile(path.join(root, 'sample.txt'), 'utf8')).toBe('user header\nvalue = "beta-native"\nkeep existing edit\n');
    expect(await fs.readFile(path.join(root, '.git/index'))).toEqual(index);
    await window.getByRole('button', { name: '修改与验证', exact: true }).click();
    const review = window.getByTestId('review-panel');
    await expect(review.getByTestId('frozen-checks')).toContainText('已记录命令成功');
    await expect(review.getByLabel('冻结 Diff 文本')).toContainText('+value = "beta-native"');
    const checks = query('runs.checks', { run_id: runId }).items;
    expect(checks).toHaveLength(1); expect(checks[0]).toMatchObject({ status: 'passed', exit_code: 0, command: 'sh verify.sh' });
    await review.getByRole('tab', { name: '验证记录' }).click();
    await expect(review.getByRole('heading', { name: '已记录命令成功', exact: true })).toBeVisible();
    await window.screenshot({ path: 'test-results/live-deepseek-review.png' });
    const workflowId = runId;
    await window.getByLabel('发送任务', { exact: true }).fill('Use run_shell exactly once with command "sleep 20" and purpose "verification". Do not edit any files.');
    await window.getByRole('button', { name: '发送任务', exact: true }).click();
    await expect(window.getByTestId('approval-card')).toContainText('sleep 20', { timeout: 70000 });
    runId = query('runs.list')[0].run_id;
    expect(query('runs.get', { run_id: runId }).session_id).toBe(sessionId);
    await window.getByRole('button', { name: '批准', exact: true }).click();
    await expect.poll(() => query('runs.checks', { run_id: runId }).items.some((c: { command_outcome: string }) => c.command_outcome === 'running')).toBe(true);
    await window.getByRole('button', { name: '取消运行', exact: true }).click();
    await expect(window.getByTestId('run-status')).toHaveText('已取消');
    await expect(review.getByTestId('frozen-checks')).toContainText('检查已取消');
    const cancelled = query('runs.checks', { run_id: runId }).items;
    expect(cancelled[0]).toMatchObject({ status: 'cancelled', cancelled: true });
    await fs.writeFile('test-results/live-deepseek.json', JSON.stringify({ status: 'passed', provider: configuration.provider,
      model: configuration.model, endpoint: configuration.endpoint, actual_network: true, fixture: false, packaged: true, global_model_settings: true, project_dotenv: false,
      workflow_run: workflowId, cancelled_run: runId, approvals: 3, reopened_waiting_run: true,
      dirty_worktree_and_index_preserved: true, frozen_review: 'ready', command: checks[0].command, exit_code: checks[0].exit_code,
      verification_status: checks[0].status, cancelled_verification_status: cancelled[0].status, continued_same_session: true }, null, 2));
  } finally {
    await application?.close().catch(() => {});
    try {
      for (const run of query('runs.list')) if (!['completed', 'stopped', 'failed', 'cancelled', 'interrupted'].includes(run.status)) query('runs.cancel', { run_id: run.run_id });
      query('host.stop');
    } catch { /* Cleanup only this test's generated workspace. */ }
    await fs.rm(root, { recursive: true, force: true }); await fs.rm(profile, { recursive: true, force: true }); await portable.remove();
  }
});
