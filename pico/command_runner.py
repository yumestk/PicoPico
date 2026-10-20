"""POSIX shell process groups with bounded, continuously drained output."""

import os
import selectors
import signal
import subprocess
import time


MAX_OUTPUT_BYTES = 64 * 1024


def _terminate(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    started = time.monotonic()
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        pass
    time.sleep(max(0, 0.2 - (time.monotonic() - started)))
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_command(command, cwd, env, timeout=20, cancel_event=None, max_output_bytes=MAX_OUTPUT_BYTES):
    started = time.monotonic()
    data = {'exit_code': None, 'stdout': '', 'stderr': '', 'stdout_truncated': False,
            'stderr_truncated': False, 'timed_out': False, 'cancelled': False, 'duration_ms': 0}
    if os.name != 'posix':
        data['stderr'] = 'Process-group command execution requires POSIX on this version of Pico.'
        return data
    if cancel_event is not None and cancel_event.is_set():
        data['cancelled'] = True
        return data
    try:
        process = subprocess.Popen(command, shell=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, bufsize=0)
    except OSError as exc:
        data['stderr'] = str(exc)
        return data
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    terminal_at = None
    try:
        with selectors.DefaultSelector() as selector:
            for name, stream in [('stdout', process.stdout), ('stderr', process.stderr)]:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map() or process.poll() is None:
                cancelled = cancel_event is not None and cancel_event.is_set()
                timed_out = time.monotonic() - started >= timeout
                if (cancelled or timed_out) and not (data['cancelled'] or data['timed_out']):
                    data['cancelled'], data['timed_out'] = cancelled, not cancelled
                    terminal_at = time.monotonic()
                    _terminate(process)
                for key, _ in selector.select(timeout=0.05):
                    try:
                        chunk = os.read(key.fileobj.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    name = key.data
                    remaining = max(0, max_output_bytes - len(buffers[name]))
                    buffers[name].extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        data[name + '_truncated'] = True
                # A killed descendant must not keep inherited pipe handles open forever.
                if terminal_at is not None and time.monotonic() - terminal_at > 1:
                    break
            data['exit_code'] = process.wait()
    except BaseException:
        _terminate(process)
        raise
    finally:
        process.stdout.close()
        process.stderr.close()
    for name, buffer in buffers.items():
        data[name] = buffer.decode('utf-8', errors='ignore')
    data['duration_ms'] = int((time.monotonic() - started) * 1000)
    return data
