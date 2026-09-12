"""The `ui` command's startup lines must reach a redirected stdout.

This needs a subprocess, and the reason is the whole point of the test.
Python block-buffers stdout when it is not a terminal, and that buffering
lives in the real file object wrapping a real pipe. Redirecting
`sys.stdout` to a StringIO inside the test process does not reproduce it:
StringIO has no buffering to get wrong, so an unflushed print would pass.

`uvicorn.run` is replaced in the child with a blocking read rather than
stubbed out, because the bug only shows while the server is still
running. If the call returns, interpreter shutdown flushes the buffer and
the missing flush becomes invisible. Blocking also means no port is
bound and no server starts.
"""
import subprocess
import sys
import threading

CHILD = """
import argparse, sys
import uvicorn
uvicorn.run = lambda *a, **k: sys.stdin.read()
from rephemeral.cli import cmd_ui
cmd_ui(argparse.Namespace(bind='127.0.0.1', port=8765, reload={reload}))
"""


def _first_line(reload: bool, timeout: float = 10.0) -> str:
    """Start the child, return its first stdout line, then kill it."""
    child = subprocess.Popen(
        [sys.executable, '-c', CHILD.format(reload=reload)],
        stdout=subprocess.PIPE,
        stdin=subprocess.PIPE,
        text=True,
    )
    captured = []

    def read():
        line = child.stdout.readline()
        captured.append(line)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(timeout)
    child.kill()
    child.wait(timeout=timeout)
    # An empty list means readline never returned: the line was written
    # into a buffer nobody flushed, which is the regression.
    assert captured, (
        'no startup line within %gs; stdout was buffered and never flushed'
        % timeout
    )
    return captured[0]


def test_version_line_is_flushed_into_a_pipe():
    assert 'rephemeral' in _first_line(reload=False).lower()


def test_reload_notice_is_flushed_into_a_pipe():
    # The reload path prints a second line before handing off to uvicorn,
    # and it is the same hazard: uvicorn.run never returns.
    child = subprocess.Popen(
        [sys.executable, '-c', CHILD.format(reload=True)],
        stdout=subprocess.PIPE,
        stdin=subprocess.PIPE,
        text=True,
    )
    lines = []

    def read():
        for _ in range(2):
            lines.append(child.stdout.readline())

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(10.0)
    child.kill()
    child.wait(timeout=10.0)
    assert len(lines) == 2, 'reload path did not flush both startup lines'
    assert 'auto-reload on' in lines[1]
