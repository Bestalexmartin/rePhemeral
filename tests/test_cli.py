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
import argparse
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from rephemeral import cli, config, migrate

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
        f'no startup line within {timeout:g}s; stdout was buffered and never flushed'
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


@pytest.fixture
def offline(monkeypatch):
    """cli with the tablet, the store and migration replaced."""
    device = MagicMock()
    device.__enter__.return_value = device
    store = MagicMock()
    monkeypatch.setattr(cli.config, 'load', lambda: config.Config())
    monkeypatch.setattr(cli, '_connect', lambda cfg: device)
    monkeypatch.setattr(cli, '_store', lambda d: (store, SimpleNamespace(build='1')))
    monkeypatch.setattr(cli.migrate, 'run', lambda: migrate.Report())
    return store


BATCHES = [
    ({'suspended': 'captured', 'poweroff': 'already backed up'}, 0),
    ({'suspended': 'captured', 'poweroff': 'FAILED: disconnected'}, 1),
]


@pytest.mark.parametrize('results, code', BATCHES)
def test_backup_exit_code_reports_any_failed_screen(offline, results, code):
    offline.capture_all.return_value = results
    assert cli.main(['backup']) == code


@pytest.mark.parametrize('results, code', BATCHES)
def test_restore_all_exit_code_reports_any_failed_screen(offline, monkeypatch, results, code):
    applier = MagicMock()
    applier.return_value.restore_all.return_value = results
    monkeypatch.setattr(cli, 'Applier', applier)
    assert cli.cmd_restore(argparse.Namespace(all=True, screen=None, restart=False)) == code
    assert cli.main(['restore', '--all']) == code


@pytest.mark.parametrize('bind, url', [
    ('127.0.0.1', 'http://127.0.0.1:8765'),
    ('::1', 'http://[::1]:8765'),
])
def test_startup_line_is_a_usable_url(monkeypatch, capsys, bind, url):
    import uvicorn
    monkeypatch.setattr(uvicorn, 'run', lambda *args, **kwargs: None)
    cli.cmd_ui(argparse.Namespace(bind=bind, port=8765, reload=False))
    assert capsys.readouterr().out.split()[-1] == url
