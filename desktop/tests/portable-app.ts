import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

/** Exercise an app outside the checkout even when the caller uses the default build. */
export async function portableApp() {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'Pico portable '));
  const bundle = path.join(directory, 'Pico.app');
  await fs.cp(path.resolve(process.env.PICO_TEST_APP ?? 'out/Pico-darwin-arm64/Pico.app'), bundle,
    { recursive: true, verbatimSymlinks: true });
  return { bundle, remove: () => fs.rm(directory, { recursive: true, force: true }) };
}
