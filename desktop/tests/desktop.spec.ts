import { test, expect, _electron as electron } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createServer } from 'node:http';

test('packaged window bootstraps an independent Host and quitting only detaches', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'pico-desktop-'));
  const userData = await fs.mkdtemp(path.join(os.tmpdir(), 'pico-gui-profile-'));
  const bundle = path.resolve(process.env.PICO_TEST_APP ?? 'out/Pico-darwin-arm64/Pico.app');
  const python = path.join(bundle, 'Contents/Resources/backend/python/bin/python3');
  await fs.writeFile(path.join(directory, 'pico.py'), 'raise RuntimeError("Target repository must not be imported")\n');
  let requests = 0;
  const model = createServer((request, response) => {
    request.resume();
    request.on('end', () => {
      const content = requests++ === 0
        ? [{ type: 'tool_use', id: 'native-shell', name: 'run_shell', input: { command: "printf ready > started; sleep 1; printf finished > native-proof" } }]
        : [{ type: 'text', text: 'Native background task completed.' }];
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ content, stop_reason: content[0].type === 'tool_use' ? 'tool_use' : 'end_turn' }));
    });
  });
  await new Promise<void>(resolve => model.listen(0, '127.0.0.1', resolve));
  const address = model.address() as { port: number };
  const query = (code: string, ...args: string[]) => execFileSync(python, ['-I', '-c', code, directory, ...args],
    { cwd: directory, encoding: 'utf8', env: { PATH: '/usr/bin:/bin', HOME: os.homedir() } });
  const application = await electron.launch({
    executablePath: path.join(bundle, 'Contents/MacOS/Pico'),
    args: [`--user-data-dir=${userData}`],
    cwd: directory,
    env: { PATH: '/usr/bin:/bin:/usr/sbin:/sbin', HOME: os.homedir(),
      TMPDIR: os.tmpdir(), PYTHONPATH: directory },
  });
  let closed = false;
  try {
    const window = await application.firstWindow();
    await expect(window.getByRole('heading', { name: '从一个工作区开始。' })).toBeVisible();
    await application.evaluate(({ dialog }, directory) => {
      dialog.showOpenDialog = (async () => ({ canceled: false, filePaths: [directory] })) as typeof dialog.showOpenDialog;
    }, directory);
    await window.getByRole('button', { name: '选择文件夹 →' }).click();
    await expect(window.getByTestId('workspace-path')).toHaveText(await fs.realpath(directory));
    await expect(window.getByText('pico-host-v1', { exact: true })).toBeVisible();
    expect(await window.evaluate(() => ({ node: typeof (globalThis as unknown as { require?: unknown }).require,
      keys: Object.keys(window.pico).sort() }))).toEqual({ node: 'undefined', keys: ['chooseWorkspace', 'refreshWorkspace'] });
    await window.screenshot({ path: 'test-results/connected.png' });
    const run = JSON.parse(query('import json,sys;from pico.host_client import HostClient;c=HostClient(sys.argv[1]);s=c.call("sessions.create",{"config":{"provider":"anthropic","base_url":sys.argv[2],"approval":"auto"}});print(json.dumps(c.call("runs.start",{"session_id":s["id"],"prompt":"Run the requested shell once","client_request_id":"desktop-smoke"})))', `http://127.0.0.1:${address.port}`));
    await expect.poll(async () => fs.readFile(path.join(directory, 'started'), 'utf8').catch(() => '')).toBe('ready');
    await application.close();
    closed = true;
    const state = execFileSync(python, ['-I', '-c',
      'import json,sys;from pico.host_client import HostClient;c=HostClient(sys.argv[1]);print(json.dumps(c.call("host.info")))', directory],
      { cwd: directory, encoding: 'utf8', env: { PATH: '/usr/bin:/bin', HOME: os.homedir() } });
    expect(JSON.parse(state).status).toBe('ready');
    await expect.poll(() => JSON.parse(query('import json,sys;from pico.host_client import HostClient;print(json.dumps(HostClient(sys.argv[1]).call("runs.get",{"run_id":sys.argv[2]})))', run.run_id)).status).toBe('completed');
    expect(await fs.readFile(path.join(directory, 'native-proof'), 'utf8')).toBe('finished');
    expect(requests).toBe(2);
    await fs.writeFile('test-results/runtime.json', JSON.stringify({ host_survived_exit: true, native_model_requests: requests,
      worker_guardian_shell: 'passed', isolated_from_target_imports: true, minimal_path: true, bundle }, null, 2));
  } finally {
    if (!closed) await application.close();
    try { execFileSync(python, ['-I', '-c', 'import sys;from pico.host_client import HostClient;HostClient(sys.argv[1]).call("host.stop")', directory]); } catch { /* Reported by primary assertions. */ }
    await new Promise<void>(resolve => model.close(() => resolve()));
    await fs.rm(directory, { recursive: true, force: true });
    await fs.rm(userData, { recursive: true, force: true });
  }
});
