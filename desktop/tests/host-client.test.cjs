const { test } = require('node:test');
const assert = require('node:assert/strict');
const net = require('node:net');
const os = require('node:os');
const fs = require('node:fs/promises');
const path = require('node:path');
const { callHost, hostInfo } = require('../build/main/host-client');

async function endpoint(t, respond) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'pico-node-'));
  const socket = path.join(root, 'test.sock');
  const server = net.createServer(connection => connection.once('data', raw => respond(connection, JSON.parse(raw))));
  await new Promise(resolve => server.listen(socket, resolve));
  t.after(async () => { await new Promise(resolve => server.close(resolve)); await fs.rm(root, { recursive: true }); });
  return socket;
}

test('fragmented UTF-8 response preserves text and envelope identity', async t => {
  const socket = await endpoint(t, (connection, request) => {
    assert.equal(request.protocol, 'pico-host-v1');
    const raw = Buffer.from(JSON.stringify({ id: request.id, result: '中文🙂' }) + '\n');
    connection.write(raw.subarray(0, raw.length - 5));
    setTimeout(() => connection.end(raw.subarray(raw.length - 5)), 5);
  });
  assert.equal(await callHost(socket, '/workspace', 'host.info'), '中文🙂');
});

test('mismatched IDs, workspace, oversized responses and disconnects fail closed', async t => {
  for (const mode of ['id', 'workspace', 'oversized', 'eof', 'error', 'timeout']) {
    await t.test(mode, async t => {
      const socket = await endpoint(t, (connection, request) => {
        connection.on('error', () => {});
        if (mode === 'oversized') connection.end(Buffer.alloc(1024 * 1024 + 1, 65));
        else if (mode === 'eof') connection.end();
        else if (mode === 'timeout') connection.on('end', () => connection.end());
        else connection.end(JSON.stringify(mode === 'error'
          ? { id: request.id, error: { code: 'invalid_request', message: 'Rejected' } }
          : { id: mode === 'id' ? 'wrong' : request.id, result: { protocol: 'pico-host-v1', workspace: '/wrong', instance_id: 'host' } }) + '\n');
      });
      if (mode === 'workspace') await assert.rejects(hostInfo(socket, '/workspace'), /mismatch/);
      else await assert.rejects(callHost(socket, '/workspace', 'host.info', {}, 50));
    });
  }
});
