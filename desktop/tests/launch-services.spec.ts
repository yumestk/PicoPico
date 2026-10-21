import { test, expect, chromium } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { portableApp } from './portable-app';

test('moved application opens through macOS LaunchServices', async () => {
  const portable = await portableApp();
  const { bundle } = portable;
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), 'pico-launch-services-'));
  let browser: Awaited<ReturnType<typeof chromium.connectOverCDP>> | undefined;
  let pid: number | undefined;
  try {
    const started = performance.now();
    execFileSync('/usr/bin/open', ['-n', bundle, '--args', `--user-data-dir=${profile}`, '--remote-debugging-port=0'],
      { env: { HOME: os.homedir(), PATH: '/usr/bin:/bin' } });
    let port = '';
    await expect.poll(async () => {
      port = await fs.readFile(path.join(profile, 'DevToolsActivePort'), 'utf8').then(value => value.split('\n')[0], () => '');
      return port;
    }, { timeout: 15000 }).not.toBe('');
    browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
    const session = await browser.newBrowserCDPSession();
    const processes = await session.send('SystemInfo.getProcessInfo');
    pid = processes.processInfo.find(value => value.type === 'browser')?.id;
    const window = browser.contexts()[0].pages()[0];
    await expect(window.getByRole('heading', { name: '从一个工作区开始。' })).toBeVisible();
    const launchMs = Math.round(performance.now() - started);
    expect(window.url()).toBe('pico://app/index.html');
    await window.screenshot({ path: 'test-results/launch-services.png' });
    await fs.writeFile('test-results/launch-services.json', JSON.stringify({ passed: true, launch: 'macOS LaunchServices',
      bundle, page: window.url(), external_server: false, launch_to_welcome_ms: launchMs }, null, 2));
  } finally {
    // Only terminate the live test process if it still carries this unique test profile.
    if (pid) {
      try {
        const command = execFileSync('/bin/ps', ['-p', String(pid), '-o', 'command='], { encoding: 'utf8' });
        if (command.includes(profile)) process.kill(pid, 'SIGTERM');
      } catch { /* LaunchServices has already reaped the owned process. */ }
    }
    await browser?.close().catch(() => {});
    await fs.rm(profile, { recursive: true, force: true });
    await portable.remove();
  }
});
