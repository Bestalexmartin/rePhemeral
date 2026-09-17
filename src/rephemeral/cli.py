"""Command line interface for rePhemeral."""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from . import config, hostkey, inspector, migrate, screens
from .apply import Applier
from .backup import BackupError, BackupStore
from .device import Device, DeviceError
from .images import FIT_MODES, ImageError


def _connect(cfg: config.Config) -> Device:
    if not cfg.key.is_file():
        raise SystemExit(
            f"No SSH key at {cfg.key}. Run `rephemeral setup` first."
        )
    # Create the host key store before connecting, so it is private from
    # the start. Left to paramiko, it would be written on first contact at
    # whatever the umask allows.
    hostkey.ensure_store()
    return Device(host=cfg.host, key_path=str(cfg.key),
                  username=cfg.username, port=cfg.port)


def _store(device: Device) -> tuple[BackupStore, object]:
    info = device.info()
    return BackupStore(device, build=info.build, board=info.board), info


def _masked_input(prompt: str, read_char, write) -> str:
    """Collect a password a keystroke at a time, echoing an asterisk for each.

    Kept apart from the terminal handling so the key logic can be tested
    without a terminal. `read_char` returns one character, "" at end of
    input, or None for a key that produced no character.
    """
    write(prompt)
    chars: list[str] = []
    while True:
        ch = read_char()
        if ch is None:
            continue
        if ch in ("\r", "\n"):
            write("\n")
            return "".join(chars)
        if ch == "\x03":
            write("\n")
            raise KeyboardInterrupt
        if ch in ("", "\x04"):
            write("\n")
            raise EOFError
        if ch in ("\x08", "\x7f"):
            if chars:
                chars.pop()
                write("\b \b")
            continue
        if ch < " ":
            continue
        chars.append(ch)
        write("*")


def _read_password(prompt: str) -> str:
    """Prompt for a password, echoing an asterisk per keystroke.

    getpass echoes nothing at all, which on a first run reads as a prompt
    that has stopped taking input. Without an interactive terminal there
    is nothing to echo to, so that case still goes through getpass.
    """
    if not sys.stdin.isatty():
        return getpass.getpass(prompt)

    def write(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    if os.name == "nt":
        import msvcrt

        def read_char() -> str | None:
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                # Arrow and function keys arrive as a prefix then a key code,
                # and neither belongs in a password.
                msvcrt.getwch()
                return None
            return ch

        return _masked_input(prompt, read_char, write)

    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        # cbreak clears ECHO and ICANON, so keys arrive one at a time and
        # unechoed, while leaving ISIG alone so Ctrl+C still interrupts.
        tty.setcbreak(fd)
        return _masked_input(prompt, lambda: sys.stdin.read(1), write)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def cmd_setup(args: argparse.Namespace) -> int:
    cfg = config.load()
    cfg.host = args.host or cfg.host
    key = config.ensure_key(cfg.key)
    # Only the tool's own location. A directory chosen with
    # REPHEMERAL_CONFIG_DIR may be shared on purpose, and locking someone
    # else out of it is not setup's business.
    if not os.environ.get("REPHEMERAL_CONFIG_DIR"):
        widened = config.restrict_existing_dir(config.CONFIG_DIR)
        if widened:
            print(f"Restricted {config.CONFIG_DIR}, which could be written by "
                  f"{', '.join(widened)}. They could have replaced the key "
                  f"without ever reading it.")
    store = hostkey.ensure_store()
    # Read before connecting: the connection records the key on first
    # contact, so afterwards there is no telling which run did it.
    known = hostkey.recorded(cfg.host, cfg.port)
    known_fp = hostkey.fingerprint(known) if known is not None else None
    if args.trust_new_key and known is None:
        print(f"No host key is recorded for {cfg.host}; the next connection "
              f"records whatever it offers.")
    # Compare the offered key before anyone types anything. The key
    # exchange happens before authentication, so this sends nothing, and a
    # mismatch stops setup at the door rather than after the prompt.
    # Unreachable is not a refusal: the connection later says so in its own
    # words, and refusing here would turn "the cable is out" into an alarm.
    if known is not None and not args.trust_new_key:
        try:
            offered_fp = hostkey.fingerprint(hostkey.offered(cfg.host, cfg.port,
                                                             timeout=8.0))
        except DeviceError:
            offered_fp = None
        if offered_fp is not None and offered_fp != known_fp:
            print(f"The host at {cfg.host} offers an SSH host key that is not "
                  f"the one recorded for it.\n"
                  f"  recorded: {known_fp}\n"
                  f"  offered:  {offered_fp}\n"
                  f"Nothing was sent to it, and you were not asked for a "
                  f"password. If you reset the tablet or enabled developer "
                  f"mode, `rephemeral trust-key` records the new key.",
                  file=sys.stderr)
            return 1
    print(f"Keypair: {key}")
    print(f"Public:  {config.public_key_line(key)}")
    print()
    print("The tablet's root password is shown on the device under Settings.")
    print("It is used once, to install the key above, and is never stored.")
    try:
        password = _read_password("Device root password: ")
    except KeyboardInterrupt:
        print("Aborted.", file=sys.stderr)
        return 1
    except EOFError:
        # Not the same as someone pressing Ctrl+C: the terminal gave this
        # process no input at all, which is worth saying rather than
        # reporting as a deliberate abort.
        # Not "nothing has been changed": by this point setup may have
        # generated the keypair and restricted the configuration folder.
        # What it has certainly not done is talk to the tablet.
        print("No password could be read: this terminal provided no input. "
              "Nothing was sent to the tablet and no key was installed. The "
              "keypair and the configuration folder are as the lines above "
              "left them.", file=sys.stderr)
        return 1
    if not password:
        print("No password entered; aborted.", file=sys.stderr)
        return 1
    # Forget only now, with a password in hand. Forgetting earlier meant
    # that anything failing in between, including a prompt that never got
    # to read, left the tablet un-pinned without saying so.
    if args.trust_new_key and known is not None:
        hostkey.forget(cfg.host, cfg.port)
        print(f"Forgot the host key recorded for {cfg.host} ({known_fp}).")
    try:
        config.install_key(password, host=cfg.host, username=cfg.username,
                           port=cfg.port, key_path=cfg.key)
    except Exception as exc:
        if args.trust_new_key and known is not None:
            hostkey.record(cfg.host, known, cfg.port)
            print(f"Setup failed, so the host key recorded before it "
                  f"({known_fp}) has been put back.", file=sys.stderr)
        print(f"Could not install the key: {exc}", file=sys.stderr)
        return 1
    finally:
        del password
    cfg.key_path = str(key)
    saved = cfg.save()
    print(f"Key installed. Config written to {saved}")
    with _connect(cfg) as d:
        info = d.info()
        print(f"Connected to {info.board}, firmware build {info.build}")
    now = hostkey.recorded_fingerprint(cfg.host, cfg.port)
    if now and now != known_fp:
        print(f"Recorded the tablet's host key in {store}:")
        print(f"  {now}")
        print("From now on a different key stops the tool rather than "
              "connecting. After a factory reset, or after enabling developer "
              "mode, `rephemeral trust-key` records the new one without "
              "asking for a password.")
    elif now and args.trust_new_key:
        # Re-trusted, and the tablet offered what it offered before. Worth
        # saying plainly, since the run began by forgetting a key.
        print(f"Recorded the same host key again: {now}")
    elif now:
        print(f"Host key unchanged: {now}")
    return 0


def cmd_trust_key(args: argparse.Namespace) -> int:
    """Record the host key the tablet offers now, without a password.

    A factory reset regenerates the tablet's key and developer mode forces
    one. Before this, the only way back was `setup --trust-new-key`, which
    reinstalls the keypair and so asks for the root password, even though
    the key it installs is already there.
    """
    cfg = config.load()
    cfg.host = args.host or cfg.host
    store = hostkey.ensure_store()
    known = hostkey.recorded(cfg.host, cfg.port)
    known_fp = hostkey.fingerprint(known) if known is not None else None
    try:
        offered = hostkey.offered(cfg.host, cfg.port, timeout=8.0)
    except DeviceError as exc:
        print(f"{exc}", file=sys.stderr)
        print("Nothing was changed.", file=sys.stderr)
        return 1
    offered_fp = hostkey.fingerprint(offered)
    if offered_fp == known_fp:
        print(f"{cfg.host} offers the key already recorded: {offered_fp}")
        print("Nothing to do.")
        return 0
    hostkey.record(cfg.host, offered, cfg.port)
    if known_fp is None:
        print(f"Recorded the host key for {cfg.host} in {store}:")
        print(f"  {offered_fp}")
    else:
        print(f"Replaced the host key recorded for {cfg.host} in {store}:")
        print(f"  was: {known_fp}")
        print(f"  now: {offered_fp}")
        print("If you did not reset the tablet or enable developer mode, "
              "something else is answering at that address.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = config.load()
    with _connect(cfg) as d:
        store, info = _store(d)
        print(f"Device    {info.board} at {cfg.host}")
        print(f"Firmware  build {info.build}, kernel {info.kernel}")
        print(f"Rootfs    {info.free_mb:.1f} MB free of "
              f"{info.total_bytes / 1024 / 1024:.0f} MB")
        print(f"Backups   {store.host_dir}")
        print()
        print(f"{'screen':<16} {'state':<45} size")
        for key, state in store.drift(screens.SCREENS).items():
            s = screens.get(key)
            print(f"{key:<16} {state:<45} {s.width}x{s.height}")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    cfg = config.load()
    with _connect(cfg) as d:
        store, info = _store(d)
        print(f"Capturing stock screens for build {info.build}")
        results = store.capture_all(screens.SCREENS,
                                    assume_stock=args.assume_stock)
        for key, status in results.items():
            print(f"  {key:<16} {status}")
        print(f"\nHost copy:   {store.host_dir}")
        print(f"Device copy: {store.device_dir}")
    return int(any(value.startswith("FAILED:") for value in results.values()))


def cmd_set(args: argparse.Namespace) -> int:
    cfg = config.load()
    screen = screens.get(args.screen)
    path = Path(args.image)
    if not path.is_file():
        print(f"No such file: {path}", file=sys.stderr)
        return 1
    if screen.risky and not args.force:
        print(f"{screen.label} is a diagnostic screen: {screen.description}")
        print("Re-run with --force if you are sure.", file=sys.stderr)
        return 1

    with _connect(cfg) as d:
        store, _ = _store(d)
        applier = Applier(d, store)
        result = applier.apply(
            screen, path.read_bytes(), source=path.name,
            fit=args.fit, grayscale=args.grayscale,
        )
        if result.backed_up_now:
            print(f"Stock {screen.key} captured to backup first.")
        for n in result.notes:
            print(f"  - {n}")
        print(f"Wrote {screen.path} ({result.size_bytes / 1024:.0f} KB)")
        print(f"  sha256 {result.sha256[:16]}")
        if args.restart:
            applier.restart_ui()
            print("Restarted xochitl.")
        else:
            print("Live now. Sleep and wake the tablet to see a new sleep screen.")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    cfg = config.load()
    with _connect(cfg) as d:
        store, _ = _store(d)
        applier = Applier(d, store)
        if args.all:
            results = applier.restore_all(screens.SCREENS)
            for key, status in results.items():
                print(f"  {key:<16} {status}")
        else:
            if not args.screen:
                print("Give a screen name, or --all", file=sys.stderr)
                return 1
            screen = screens.get(args.screen)
            sha = applier.restore(screen)
            print(f"Restored {screen.key} (sha256 {sha[:16]})")
        if args.restart:
            applier.restart_ui()
            print("Restarted xochitl.")
    return int(args.all and any(value.startswith("FAILED:") for value in results.values()))


def cmd_screens(args: argparse.Namespace) -> int:
    for s in screens.SCREENS:
        flag = "  [diagnostic]" if s.risky else ""
        print(f"{s.key:<16} {s.width}x{s.height}  {s.label}{flag}")
        print(f"{'':<16} {s.description}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """Report on every layer between here and the tablet."""
    checks = inspector.run()
    width = max(len(c.name) for c in checks)
    for c in checks:
        print(f"  {c.status:<4}  {c.name:<{width}}  {c.detail}")
    failed = [c for c in checks if c.status == inspector.FAIL]
    warned = [c for c in checks if c.status == inspector.WARN]
    print()
    if failed:
        print(f"{len(failed)} failed, {len(warned)} warning(s). "
              f"The first failure is usually the only real one; checks that "
              f"depend on a failed one are skipped rather than run.")
        return 1
    if warned:
        print(f"No failures, {len(warned)} warning(s).")
        return 0
    print("All checks passed.")
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("The web UI needs uvicorn: pip install rephemeral", file=sys.stderr)
        return 1

    from . import __version__

    # Printed so a stale process is visible. The page HTML is read from disk
    # per request while routes are imported once, so an old server serves
    # new markup against old endpoints, which looks like a broken feature
    # rather than a stale server.
    #
    # Flushed explicitly because uvicorn.run() below never returns. Python
    # block-buffers stdout when it is not a terminal, so redirected into a
    # log file these lines would sit unwritten for the life of the server,
    # which is exactly the case where you are reading a log to find out
    # which version is running.
    # An IPv6 literal needs brackets in a URL, or the port reads as part of it.
    host = f"[{args.bind}]" if ":" in args.bind else args.bind
    print(f"rePhemeral {__version__} on http://{host}:{args.port}", flush=True)

    if not args.reload:
        from .web import app
        uvicorn.run(app, host=args.bind, port=args.port, log_level="warning")
        return 0

    # Reload is opt-in, not the default. The reloader restarts the server
    # whenever a watched file changes, and a restart during a write would
    # drop the SSH session mid-remount, leaving the tablet's root
    # filesystem writable. That is a development convenience, not something
    # to impose on someone replacing a sleep screen.
    package = Path(__file__).resolve().parent
    print(f"  auto-reload on, watching {package}", flush=True)
    uvicorn.run(
        # Reload needs an import string: the worker is a subprocess and has
        # to import the app itself.
        "rephemeral.web:app",
        host=args.bind,
        port=args.port,
        log_level="warning",
        reload=True,
        reload_dirs=[str(package)],
        # Static files are re-read from disk per request, so a restart for
        # them would be churn with no effect.
        reload_excludes=["*.html", "*.png"],
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="rephemeral",
        description="Replace the sleep and restart screens on a reMarkable Paper Pro.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("setup", help="generate a key and install it on the tablet")
    s.add_argument("--host", default=None)
    s.add_argument("--trust-new-key", action="store_true",
                   help="record the host key the tablet offers now, in place of "
                        "the one on file. A factory reset regenerates the "
                        "tablet's key, and so does enabling developer mode.")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("trust-key",
                       help="record the host key the tablet offers now")
    s.add_argument("--host", default=None)
    s.set_defaults(func=cmd_trust_key)

    s = sub.add_parser("status", help="show the device and what is on each screen")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("backup", help="capture the stock screens")
    s.add_argument("--assume-stock", action="store_true",
                   help="capture screens that were modified after the firmware "
                        "was installed. Only say this if you know they are the "
                        "originals, for instance because you restored them.")
    s.set_defaults(func=cmd_backup)

    s = sub.add_parser("screens", help="list the replaceable screens")
    s.set_defaults(func=cmd_screens)

    s = sub.add_parser("set", help="replace one screen with an image")
    s.add_argument("screen")
    s.add_argument("image")
    s.add_argument("--fit", default="cover", choices=FIT_MODES)
    s.add_argument("--grayscale", action="store_true")
    s.add_argument("--restart", action="store_true",
               help="restart the tablet UI after (not needed for screen changes)")
    s.add_argument("--force", action="store_true", help="allow diagnostic screens")
    s.set_defaults(func=cmd_set)

    s = sub.add_parser("restore", help="put stock screens back")
    s.add_argument("screen", nargs="?")
    s.add_argument("--all", action="store_true")
    s.add_argument("--restart", action="store_true")
    s.set_defaults(func=cmd_restore)

    s = sub.add_parser("inspector",
                       help="diagnose the environment, the link and the tablet")
    s.set_defaults(func=cmd_inspect)

    s = sub.add_parser("ui", help="run the local web interface")
    s.add_argument("--bind", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--reload", action="store_true",
                   help="restart on source changes (development; a restart "
                        "mid-write would drop the SSH session)")
    s.set_defaults(func=cmd_ui)

    args = p.parse_args(argv)
    try:
        report = migrate.run()
    except OSError as exc:
        print(f"Could not copy rePhemeral's files from their previous location: {exc}. "
              f"The originals are untouched.", file=sys.stderr)
        return 2
    for line in report.copied:
        print(f"Copied {line}. The originals are left in place; delete them once "
              f"you are satisfied.", file=sys.stderr)
    for line in report.problems:
        print(line, file=sys.stderr)
    try:
        return args.func(args)
    except DeviceError as exc:
        print(f"Device error: {exc}", file=sys.stderr)
        return 2
    except (BackupError, ImageError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyError as exc:
        print(exc.args[0] if exc.args else str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
