"""POSIX shell process groups with bounded, continuously drained output."""

import json
import os
import selectors
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

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


def run_command(
    command,
    cwd,
    env,
    timeout=20,
    cancel_event=None,
    max_output_bytes=MAX_OUTPUT_BYTES,
    process_started=None,
    process_stopped=None,
    execution_lock_fd=None,
):
    started = time.monotonic()
    data = {
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "stdout_truncated": False,
        "stderr_truncated": False,
        "timed_out": False,
        "cancelled": False,
        "duration_ms": 0,
    }
    if os.name != "posix":
        data["stderr"] = (
            "Process-group command execution requires POSIX on this version of Pico."
        )
        return data
    if cancel_event is not None and cancel_event.is_set():
        data["cancelled"] = True
        return data
    control_read, control_write = os.pipe()
    token = uuid.uuid4().hex
    registered = False
    process = None
    try:
        inherited = [control_read] + (
            [execution_lock_fd] if execution_lock_fd is not None else []
        )
        process = subprocess.Popen(
            [
                sys.executable,
                *(["-I"] if sys.flags.isolated else []),
                *(["-B"] if sys.dont_write_bytecode else []),
                str(Path(__file__).resolve()),
                "--guard",
                str(control_read),
            ],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            bufsize=0,
            pass_fds=tuple(inherited),
        )
        os.close(control_read)
        control_read = None
        os.write(control_write, json.dumps({"command": command}).encode() + b"\n")
        if process_started is not None:
            process_started(process.pid, token)
        registered = True
        if cancel_event is None or not cancel_event.is_set():
            os.write(control_write, b"GO\n")
        else:
            os.close(control_write)
            control_write = None
    except BaseException:
        if control_read is not None:
            os.close(control_read)
        if control_write is not None:
            os.close(control_write)
        if process is not None:
            process.wait(timeout=2)
            process.stdout.close()
            process.stderr.close()
        raise
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    terminal_at = None
    try:
        with selectors.DefaultSelector() as selector:
            for name, stream in [
                ("stdout", process.stdout),
                ("stderr", process.stderr),
            ]:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map() or process.poll() is None:
                cancelled = cancel_event is not None and cancel_event.is_set()
                timed_out = time.monotonic() - started >= timeout
                if (cancelled or timed_out) and not (
                    data["cancelled"] or data["timed_out"]
                ):
                    data["cancelled"], data["timed_out"] = cancelled, not cancelled
                    terminal_at = time.monotonic()
                    data["cleanup_confirmed"] = _stop_guard(process)
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
                        data[name + "_truncated"] = True
                # A killed descendant must not keep inherited pipe handles open forever.
                if terminal_at is not None and time.monotonic() - terminal_at > 1:
                    break
            try:
                data["exit_code"] = process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                data["cleanup_confirmed"] = False
    except KeyboardInterrupt:
        # The Host uses SIGINT to wake a blocked worker. Preserve known output and
        # the guardian's real exit status after the same required cleanup path.
        data["cancelled"] = True
        data["cleanup_confirmed"] = _stop_guard(process)
        if not data["cleanup_confirmed"]:
            raise SystemExit("Command cleanup unconfirmed; workspace remains blocked")
        data["exit_code"] = process.returncode
        data["stdout_truncated"] = data["stderr_truncated"] = True
        if cancel_event is not None:
            cancel_event.set()
    except BaseException:
        if not _stop_guard(process):
            raise SystemExit("Command cleanup unconfirmed; workspace remains blocked")
        raise
    finally:
        if control_write is not None:
            os.close(control_write)
        if registered and process_stopped is not None and process.poll() is not None:
            process_stopped(token)
        process.stdout.close()
        process.stderr.close()
    for name, buffer in buffers.items():
        data[name] = buffer.decode("utf-8", errors="ignore")
    data["duration_ms"] = int((time.monotonic() - started) * 1000)
    return data


def _stop_guard(process):
    if process.poll() is not None:
        return True
    process.terminate()
    try:
        process.wait(timeout=1)
        return True
    except subprocess.TimeoutExpired:
        # The guardian retains the execution lease; do not release it by killing it blindly.
        return False


def _guard(fd):
    control = os.fdopen(fd, "rb", buffering=0)
    config = json.loads(control.readline(1024 * 1024))
    if control.readline(8) != b"GO\n":
        return 125
    stopping = [False]

    def stop(*_):
        stopping[0] = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    command = subprocess.Popen(
        config["command"], shell=True, stdin=subprocess.DEVNULL, start_new_session=True
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(control, selectors.EVENT_READ)
            while command.poll() is None and not stopping[0]:
                if selector.select(timeout=0.05) and not os.read(fd, 1):
                    stopping[0] = True
            code = command.poll()
    finally:
        # Also clean children which outlived a normally exiting shell.
        _terminate(command)
        control.close()
    return code if code is not None and code >= 0 else 130


if __name__ == "__main__":
    raise SystemExit(_guard(int(sys.argv[2])))
