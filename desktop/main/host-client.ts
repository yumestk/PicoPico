import { randomUUID } from 'node:crypto';
import net from 'node:net';
import type { HostInfo } from '../shared';

const MAX_MESSAGE = 1024 * 1024;
export const PROTOCOL = 'pico-host-v1';

/** One bounded request per connection, including independent cancellation of reads. */
export function callHost<T>(socket: string, workspace: string, method: string,
  params: Record<string, unknown> = {}, timeout = 5000, signal?: AbortSignal): Promise<T> {
  const id = randomUUID();
  const request = Buffer.from(JSON.stringify({ id, protocol: PROTOCOL, workspace, method, params }) + '\n');
  if (request.length > MAX_MESSAGE) return Promise.reject(new Error('Host request exceeds size limit'));
  return new Promise((resolve, reject) => {
    const connection = net.createConnection(socket);
    let buffer = Buffer.alloc(0);
    let done = false;
    const finish = (error?: Error, result?: T) => {
      if (done) return;
      done = true;
      signal?.removeEventListener('abort', abort);
      connection.destroy();
      if (error) reject(error); else resolve(result as T);
    };
    const abort = () => finish(new Error('Host read cancelled'));
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) { abort(); return; }
    connection.setTimeout(timeout, () => finish(new Error('Host request timed out')));
    connection.on('error', error => finish(error));
    connection.on('connect', () => connection.write(request));
    connection.on('end', () => finish(new Error('Host disconnected before replying')));
    connection.on('data', chunk => {
      if (done) return;
      if (buffer.length + chunk.length > MAX_MESSAGE) {
        finish(new Error('Host response exceeds size limit')); return;
      }
      buffer = Buffer.concat([buffer, chunk]);
      const end = buffer.indexOf(10);
      if (end < 0) return;
      try {
        const response = JSON.parse(buffer.subarray(0, end).toString('utf8'));
        if (!response || response.id !== id) throw new Error('Host response ID mismatch');
        if (response.error) throw new Error(`${response.error.code}: ${response.error.message}`);
        if (!Object.hasOwn(response, 'result')) throw new Error('Invalid Host response');
        finish(undefined, response.result);
      } catch (error) { finish(error instanceof Error ? error : new Error('Invalid Host response')); }
    });
  });
}

export async function hostInfo(socket: string, workspace: string): Promise<HostInfo> {
  const info = await callHost<HostInfo>(socket, workspace, 'host.info');
  if (info.protocol !== PROTOCOL || info.workspace !== workspace || typeof info.instance_id !== 'string') {
    throw new Error('Host protocol or workspace mismatch');
  }
  return info;
}
