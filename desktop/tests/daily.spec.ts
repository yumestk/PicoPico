import { test, expect, _electron as electron } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { createServer, type ServerResponse } from 'node:http';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { portableApp } from './portable-app';

test('daily use: automatic conversation, real incremental stream, safe Markdown, file evidence, reopen, cancellation and errors', async () => {
  test.setTimeout(120000);
  const portable = await portableApp(), base = await fs.mkdtemp(path.join(os.tmpdir(), 'pico-daily-'));
  const root = path.join(base, 'project'), profile = path.join(base, 'profile');
  await fs.mkdir(path.join(root, 'src'), { recursive: true }); await fs.mkdir(profile);
  await fs.writeFile(path.join(root, 'src/空 格.py'), 'first\nsecond\nprint("actual source")\n' + 'more\n'.repeat(400));
  const env = { PATH: '/usr/bin:/bin:/usr/sbin:/sbin', HOME: os.homedir(), TMPDIR: os.tmpdir(), PICO_CONFIG_DIR: path.join(profile, 'settings') };
  const python = path.join(portable.bundle, 'Contents/Resources/backend/python/bin/python3');
  const query = (method: string, params = {}) => JSON.parse(execFileSync(python, ['-I', '-B', '-c',
    'import json,sys;from pico.host_client import HostClient;print(json.dumps(HostClient(sys.argv[1]).call(sys.argv[2],json.loads(sys.argv[3]))))',
    root, method, JSON.stringify(params)], { env, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }));
  let requests = 0, remoteImages = 0, release = () => {}, socket: ServerResponse | undefined;
  const secret = 'daily-stream-credential-1234567890';
  const start = (response: ServerResponse) => { response.writeHead(200, { 'Content-Type': 'text/event-stream' }); response.flushHeaders(); };
  const event = (response: ServerResponse, value: unknown) => response.write('data: ' + JSON.stringify(value) + '\n\n');
  const begin = (response: ServerResponse, text: string) => {
    event(response, { type: 'message_start', message: { role: 'assistant', usage: { input_tokens: 10 } } });
    event(response, { type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' } });
    event(response, { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text } });
  };
  const end = (response: ServerResponse, reason = 'end_turn') => {
    event(response, { type: 'message_delta', delta: { stop_reason: reason }, usage: { output_tokens: 20 } });
    event(response, { type: 'message_stop' }); response.end();
  };
  const model = createServer((request, response) => {
    if (request.url?.includes('pixel')) { remoteImages++; response.end(); return; }
    let raw = ''; request.on('data', chunk => { raw += chunk; }); request.on('end', () => {
      requests++; const body = JSON.parse(raw), latest = body.messages.at(-1);
      expect(body.stream).toBe(true);
      if (latest.content === 'unauthorized') { response.writeHead(401); response.end('{"error":"fixture"}'); return; }
      start(response);
      if (latest.content === 'cancel stream') { socket = response; begin(response, '等待中的实际流式正文。'.repeat(20)); return; }
      if (latest.content === 'broken stream') {
        begin(response, '这段预览随后会断流，不能当作完成结果。'.repeat(12));
        event(response, { type: 'content_block_stop', index: 0 });
        event(response, { type: 'content_block_start', index: 1, content_block: { type: 'tool_use', id: 'broken-write', name: 'write_file', input: {} } });
        event(response, { type: 'content_block_delta', index: 1, delta: { type: 'input_json_delta', partial_json: '{"path":"must-not-exist","content":"bad"}' } });
        event(response, { type: 'content_block_stop', index: 1 }); response.end(); return;
      }
      if (typeof latest.content === 'string') {
        begin(response, '正在检查实际项目文件。'.repeat(15) + secret.slice(0, 12));
        event(response, { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: secret.slice(12) + '，只展示脱敏预览。'.repeat(10) } });
        event(response, { type: 'content_block_stop', index: 0 });
        for (const [i, tool] of [{ name: 'read_file', input: { path: 'src/空 格.py', start: 1, end: 3 } }, { name: 'write_file', input: { path: 'proof.txt', content: 'complete' } }].entries()) {
          event(response, { type: 'content_block_start', index: i + 1, content_block: { type: 'tool_use', id: `daily-${i}`, name: tool.name, input: {} } });
          const args = JSON.stringify(tool.input);
          for (const piece of [args.slice(0, 10), args.slice(10)]) event(response, { type: 'content_block_delta', index: i + 1, delta: { type: 'input_json_delta', partial_json: piece } });
          event(response, { type: 'content_block_stop', index: i + 1 });
        }
        release = () => end(response, 'tool_use');
      } else {
        begin(response, '# 文件已读\n\n依据 `src/空 格.py:3`。\n\n| 项目 | 结果 |\n| --- | --- |\n| 文件 | 已读取 |\n\n```python\nprint("safe code")\n```\n\n' +
          '<img src=x onerror="globalThis.__attack=1">\n\n![remote](http://127.0.0.1:' + (model.address() as { port: number }).port + '/pixel)\n\n[unsafe](javascript:alert(1)) [escape](../outside.txt)\n\n' + '阅读说明。\n\n'.repeat(100));
        event(response, { type: 'content_block_stop', index: 0 }); end(response);
      }
    });
  });
  await new Promise<void>(resolve => model.listen(0, '127.0.0.1', resolve));
  const imported = path.join(base, 'config.env');
  await fs.writeFile(imported, `PICO_PROVIDER=deepseek\nPICO_DEEPSEEK_MODEL=daily-flash\nPICO_DEEPSEEK_API_BASE=http://127.0.0.1:${(model.address() as { port: number }).port}\nPICO_DEEPSEEK_API_KEY=${secret}\n`);
  execFileSync(python, ['-I', '-B', '-m', 'pico.settings', 'import', '--file', imported], { env, stdio: 'pipe' });
  let application: Awaited<ReturnType<typeof electron.launch>> | undefined;
  const launch = () => electron.launch({ executablePath: path.join(portable.bundle, 'Contents/MacOS/Pico'), args: [`--user-data-dir=${profile}`], env });
  try {
    application = await launch(); let window = await application.firstWindow();
    await application.evaluate(({ dialog }, root) => { dialog.showOpenDialog = (async () => ({ canceled: false, filePaths: [root] })) as typeof dialog.showOpenDialog; }, root);
    await window.getByRole('button', { name: '选择文件夹 →' }).click();
    await expect(window.getByLabel('发送任务', { exact: true })).toBeVisible();
    expect(query('sessions.list')).toHaveLength(1);
    await expect(window.getByTestId('review-panel')).toBeHidden();
    await window.getByLabel('本次审批模式').selectOption('auto');
    await window.locator('#prompt').fill('read project'); await window.getByRole('button', { name: '发送任务', exact: true }).click();
    await expect(window.getByTestId('assistant-preview')).toContainText('正在检查实际项目文件');
    expect(query('runs.tool_calls', { run_id: query('runs.list')[0].run_id }).items).toHaveLength(0);
    expect(await fs.stat(path.join(root, 'proof.txt')).then(() => true, () => false)).toBe(false);
    expect(await window.locator('body').textContent()).not.toContain(secret);
    await window.locator('#prompt').fill('preserved draft');
    const firstRun = query('runs.list')[0].run_id;
    await application.close(); application = undefined;
    application = await launch(); window = await application.firstWindow();
    await expect(window.getByTestId('assistant-preview')).toContainText('正在检查实际项目文件');
    await expect(window.locator('#prompt')).toHaveValue('preserved draft');
    expect(query('sessions.list')).toHaveLength(1);
    release(); await expect(window.getByTestId('run-status')).toHaveText('已完成');
    await expect(window.getByTestId('assistant-preview')).toHaveCount(0);
    expect(await fs.readFile(path.join(root, 'proof.txt'), 'utf8')).toBe('complete');
    expect(query('runs.list')).toHaveLength(1);
    await expect(window.locator('.markdown table')).toHaveCount(1);
    expect(await window.locator('.markdown img').count()).toBe(0);
    expect(await window.evaluate(() => (globalThis as { __attack?: number }).__attack)).toBeUndefined();
    expect(remoteImages).toBe(0);
    await window.getByRole('button', { name: '复制代码', exact: true }).click();
    await expect(window.getByRole('button', { name: '已复制', exact: true })).toBeVisible();
    expect(await application.evaluate(({ clipboard }) => clipboard.readText())).toBe('print("safe code")\n');
    await window.getByRole('button', { name: 'src/空 格.py:3', exact: true }).click();
    const reader = window.getByRole('complementary', { name: '当前项目文件' });
    await expect(reader).toContainText('actual source'); await expect(reader).toContainText('版本相同');
    await fs.writeFile(path.join(root, 'src/空 格.py'), 'changed\n');
    await reader.getByRole('button', { name: '刷新当前文件' }).click();
    await expect(reader).toContainText('文件已变化');
    await reader.getByRole('button', { name: '关闭文件' }).click();
    const denied = await window.evaluate(async () => { try { await window.pico.openExternal('file:///etc/passwd'); return false; } catch { return true; } });
    expect(denied).toBe(true);
    await window.locator('#prompt').fill('cancel stream'); await window.getByRole('button', { name: '发送任务', exact: true }).click();
    await expect(window.getByTestId('assistant-preview')).toContainText('等待中的实际流式正文');
    await window.locator('.conversation-scroll').evaluate(el => { el.scrollTop = 0; el.dispatchEvent(new Event('scroll')); });
    await expect(window.getByRole('button', { name: '回到最新' })).toBeVisible();
    await expect.poll(() => window.locator('.conversation-scroll').evaluate(el => el.scrollTop)).toBe(0);
    await window.getByRole('button', { name: '取消运行', exact: true }).click();
    await expect(window.getByTestId('run-status')).toHaveText('已取消');
    socket?.destroy();
    await window.locator('#prompt').fill('broken stream'); await window.getByRole('button', { name: '发送任务', exact: true }).click();
    await expect(window.getByTestId('run-status')).toHaveText('失败');
    expect(await fs.stat(path.join(root, 'must-not-exist')).then(() => true, () => false)).toBe(false);
    expect(query('runs.get', { run_id: firstRun }).status).toBe('completed');
    await window.locator('#prompt').fill('unauthorized'); await window.getByRole('button', { name: '发送任务', exact: true }).click();
    await expect(window.getByTestId('run-status')).toHaveText('失败');
    await expect(window.locator('.message.assistant').last()).toContainText('模型认证失败');
    await expect(window.getByText('本地服务已连接', { exact: true })).toBeVisible();
    expect(requests).toBe(5);
    await window.screenshot({ path: 'test-results/daily.png' });
    await fs.writeFile('test-results/daily.json', JSON.stringify({ fixture: true, packaged: true, requests, automatic_session: true,
      preview_before_terminal: true, no_partial_tool_effects: true, reopen_same_run: true, draft_preserved: true, markdown_safe: true,
      code_copy: true, file_provenance: true, changed_file_detected: true, cancel_blocking_stream: true, broken_stream_no_replay: true, authentication_distinct_from_host: true }, null, 2));
  } finally {
    release(); socket?.destroy(); await application?.close().catch(() => {});
    try { query('host.stop'); } catch { /* Test cleanup only. */ }
    model.closeAllConnections(); await new Promise<void>(resolve => model.close(() => resolve()));
    await fs.rm(base, { recursive: true, force: true }); await portable.remove();
  }
});
