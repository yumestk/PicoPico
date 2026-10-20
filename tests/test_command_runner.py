import os
import shlex
import sys
import threading
import time

import pytest

from pico.command_runner import run_command

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")


def command(script):
    return shlex.join([sys.executable, "-c", script])


def test_drains_both_streams_and_truncates_utf8_safely(tmp_path):
    result = run_command(
        command(
            "import sys; sys.stdout.write('你好'*100000); sys.stderr.write('E'*100000)"
        ),
        tmp_path,
        {"PATH": os.environ["PATH"]},
        max_output_bytes=101,
    )
    assert result["exit_code"] == 0
    assert len(result["stdout"].encode()) <= 101
    assert len(result["stderr"].encode()) == 101
    assert result["stdout_truncated"] and result["stderr_truncated"]
    assert "\ufffd" not in result["stdout"]


def test_exit_codes_and_missing_commands(tmp_path):
    result = run_command("printf evidence; exit 7", tmp_path, {})
    assert result["exit_code"] == 7 and result["stdout"] == "evidence"
    missing = run_command("pico_nonexistent_command_237", tmp_path, {})
    assert missing["exit_code"] != 0 and missing["stderr"]


def test_timeout_preserves_output_and_stops_descendant_writes(tmp_path):
    child = "import time; time.sleep(1.2); open('late.txt','w').write('late')"
    parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); print('started',flush=True); time.sleep(5)"
    result = run_command(command(parent), tmp_path, {}, timeout=0.4)
    assert result["timed_out"] and not result["cancelled"]
    assert "started" in result["stdout"]
    time.sleep(1.2)
    assert not (tmp_path / "late.txt").exists()


def test_cancellation_preserves_output_and_is_bounded(tmp_path):
    event = threading.Event()

    def cancel_when_ready():
        deadline = time.monotonic() + 2
        while not (tmp_path / "ready").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        event.set()

    timer = threading.Thread(target=cancel_when_ready)
    timer.start()
    started = time.monotonic()
    try:
        result = run_command(
            command(
                "import time; print('before',flush=True); open('ready','w').write('ready'); time.sleep(10)"
            ),
            tmp_path,
            {},
            cancel_event=event,
        )
    finally:
        timer.join()
    assert result["cancelled"] and not result["timed_out"]
    assert "before" in result["stdout"]
    assert time.monotonic() - started < 3


def test_already_cancelled_command_never_starts(tmp_path):
    event = threading.Event()
    event.set()
    result = run_command("touch should_not_exist", tmp_path, {}, cancel_event=event)
    assert result["cancelled"]
    assert not (tmp_path / "should_not_exist").exists()


def test_parent_exit_still_cleans_inherited_pipes(tmp_path):
    child = "import time; time.sleep(.8); open('orphan-write','w').write('late')"
    script = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}])"
    result = run_command(command(script), tmp_path, {}, timeout=0.4)
    assert result["exit_code"] is not None
    time.sleep(0.8)
    assert not (tmp_path / "orphan-write").exists()
