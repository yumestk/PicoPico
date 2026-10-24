import { test, expect, _electron as electron } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { createServer } from 'node:http';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { portableApp } from './portable-app';

test('configure Pico once, read two projects without project credentials, persist and import settings without returning secrets', async () => {
  test.setTimeout(90000);
  const portable = await portableApp();
  const base = await fs.mkdtemp(path.join(os.tmpdir(), 'pico-global-model-'));
  const profile = path.join(base, 'profile'); await fs.mkdir(profile);
  const roots = [path.join(base, 'project-a'), path.join(base, 'project-b')];
  for (const [i, root] of roots.entries()) { await fs.mkdir(root); await fs.writeFile(path.join(root, 'README.md'), `PROJECT-${i}-CONTENT`); }
  const unrelated = 'This is unrelated dotenv syntax\nPICO_DEEPSEEK_API_BASE=https://untrusted.invalid\n';
  await fs.writeFile(path.join(roots[0], '.env'), unrelated);
  const env = { PATH: '/usr/bin:/bin:/usr/sbin:/sbin', HOME: os.homedir(), TMPDIR: os.tmpdir(), PICO_CONFIG_DIR: path.join(profile, 'settings') };
  const python = path.join(portable.bundle, 'Contents/Resources/backend/python/bin/python3');
  const query = (root: string, method: string, params = {}) => JSON.parse(execFileSync(python, ['-I', '-B', '-c',
    'import json,sys;from pico.host_client import HostClient;print(json.dumps(HostClient(sys.argv[1]).call(sys.argv[2],json.loads(sys.argv[3]))))',
    root, method, JSON.stringify(params)], { env, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }));
  let requests = 0;
  const model = createServer((request, response) => {
    let raw = ''; request.on('data', c => { raw += c; }); request.on('end', () => {
      requests++;
      expect(request.headers['x-api-key']).toBe('global-desktop-test-secret');
      const latest = JSON.parse(raw).messages.at(-1);
      const reading = typeof latest.content === 'string';
      const content = reading ? [{ type: 'tool_use', id: 'read-project', name: 'read_file', input: { path: 'README.md' } }]
        : [{ type: 'text', text: '读取结果：' + latest.content.map((c: { content: string }) => c.content).join('\n') }];
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ content, stop_reason: reading ? 'tool_use' : 'end_turn' }));
    });
  });
  await new Promise<void>(resolve => model.listen(0, '127.0.0.1', resolve));
  const endpoint = `http://127.0.0.1:${(model.address() as { port: number }).port}/anthropic`;
  let application: Awaited<ReturnType<typeof electron.launch>> | undefined;
  const launch = () => electron.launch({ executablePath: path.join(portable.bundle, 'Contents/MacOS/Pico'), args: [`--user-data-dir=${profile}`], env });
  try {
    application = await launch(); let window = await application.firstWindow();
    await window.getByRole('button', { name: 'Pico 模型设置', exact: true }).click();
    const settings = window.getByRole('dialog', { name: 'Pico 模型设置' });
    await expect(settings.getByLabel('Pico 提供方')).toBeEnabled();
    await settings.getByLabel('Pico 模型', { exact: true }).fill('deepseek-v4-flash');
    await settings.getByLabel('Pico 服务地址').fill(endpoint);
    await settings.getByLabel('Pico API key').fill('global-desktop-test-secret');
    await settings.getByRole('button', { name: '保存设置', exact: true }).click();
    await expect(settings).toHaveCount(0);
    for (const [i, root] of roots.entries()) {
      await application.evaluate(({ dialog }, root) => { dialog.showOpenDialog = (async () => ({ canceled: false, filePaths: [root] })) as typeof dialog.showOpenDialog; }, root);
      await window.getByRole('button', { name: i ? '＋ 选择工作区' : '选择文件夹 →', exact: true }).click();
      await expect(window.getByTestId('workspace-path')).toHaveText(await fs.realpath(root));
      await expect(window.locator('.session-heading')).toContainText('deepseek-v4-flash');
      await window.getByLabel('发送任务', { exact: true }).fill('Read README.md and explain this project');
      await window.getByRole('button', { name: '发送任务', exact: true }).click();
      await expect(window.getByTestId('run-status')).toHaveText('已完成');
      await expect(window.locator('.message.assistant').last()).toContainText(`PROJECT-${i}-CONTENT`);
      const run = query(root, 'runs.list')[0];
      const calls = query(root, 'runs.tool_calls', { run_id: run.run_id }).items;
      expect(calls).toHaveLength(1); expect(calls[0].name).toBe('read_file');
      expect(query(root, 'configuration.get')).toMatchObject({ model: 'deepseek-v4-flash', api_key_configured: true });
    }
    expect(requests).toBe(4);
    expect(await fs.readFile(path.join(roots[0], '.env'), 'utf8')).toBe(unrelated);
    expect(await fs.stat(path.join(roots[1], '.env')).then(() => true, () => false)).toBe(false);
    const saved = path.join(env.PICO_CONFIG_DIR, 'models.json');
    expect((await fs.stat(saved)).mode & 0o777).toBe(0o600);
    const summary = await window.evaluate(() => window.pico.modelSettings());
    expect(JSON.stringify(summary)).not.toContain('global-desktop-test-secret');
    expect(await window.evaluate(() => JSON.stringify(localStorage))).not.toContain('global-desktop-test-secret');
    await application.close(); application = undefined;
    application = await launch(); window = await application.firstWindow();
    await expect(window.getByTestId('workspace-path')).toHaveText(await fs.realpath(roots[1]));
    await window.getByRole('button', { name: 'Pico 模型设置', exact: true }).click();
    await expect(window.getByLabel('Pico API key')).toHaveValue('');
    await expect(window.getByLabel('Pico API key')).toHaveAttribute('placeholder', /已保存/);
    await window.screenshot({ path: 'test-results/global-settings-dialog.png' });
    const before = await fs.readFile(saved);
    const denied = await window.evaluate(async () => {
      try { await window.pico.saveModelSettings({ provider: 'deepseek', model: 'x', endpoint: 'https://user:private@example.invalid' }); return false; } catch { return true; }
    });
    expect(denied).toBe(true); expect(await fs.readFile(saved)).toEqual(before);
    const source = path.join(base, 'import.env');
    await fs.writeFile(source, `PICO_PROVIDER=deepseek\nPICO_DEEPSEEK_MODEL=imported-model\nPICO_DEEPSEEK_API_BASE=${endpoint}\nPICO_DEEPSEEK_API_KEY=imported-key\nDATABASE_PASSWORD=do-not-import\n`);
    await application.evaluate(({ dialog }, source) => { dialog.showOpenDialog = (async () => ({ canceled: false, filePaths: [source] })) as typeof dialog.showOpenDialog; }, source);
    await window.getByRole('button', { name: '从已有 .env 导入' }).click();
    await expect(window.getByRole('dialog')).toHaveCount(0);
    await expect.poll(() => query(roots[1], 'configuration.get').model).toBe('imported-model');
    await expect(window.locator('.session-heading')).toContainText('deepseek-v4-flash'); // Frozen old session.
    expect(await fs.readFile(saved, 'utf8')).not.toContain('do-not-import');
    await window.screenshot({ path: 'test-results/global-settings.png' });
    await fs.writeFile('test-results/global-settings.json', JSON.stringify({ shared_projects: 2, native_requests: requests,
      real_read_file: true, project_env_untouched: true, global_model_precedence: true, private_file_mode: '0600',
      key_not_returned: true, reload_preserved: true, import_model_fields_only: true, invalid_save_preserved: true,
      old_session_frozen: true, fixture: true }, null, 2));
  } finally {
    await application?.close().catch(() => {});
    for (const root of roots) { try { query(root, 'host.stop'); } catch { /* Not started. */ } }
    await new Promise<void>(resolve => model.close(() => resolve()));
    await fs.rm(base, { recursive: true, force: true }); await portable.remove();
  }
});
