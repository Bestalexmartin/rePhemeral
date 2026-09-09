"""Command line interface for rePhemeral."""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from . import config, screens
from .apply import Applier
from .backup import BackupStore
from .device import Device, DeviceError


def _connect(cfg: config.Config) -> Device:
    if not cfg.key.is_file():
        raise SystemExit(
            f"No SSH key at {cfg.key}. Run `rephemeral setup` first."
        )
    return Device(host=cfg.host, key_path=str(cfg.key),
                  username=cfg.username, port=cfg.port)


def _store(device: Device) -> tuple[BackupStore, object]:
    info = device.info()
    return BackupStore(device, build=info.build, board=info.board), info


def cmd_setup(args: argparse.Namespace) -> int:
    cfg = config.load()
    cfg.host = args.host or cfg.host
    key = config.ensure_key(cfg.key)
    print(f"Keypair: {key}")
    print(f"Public:  {config.public_key_line(key)}")
    print()
    print("The tablet's root password is shown on the device under Settings.")
    print("It is used once, to install the key above, and is never stored.")
    password = getpass.getpass("Device root password: ")
    if not password:
        print("No password entered; aborted.", file=sys.stderr)
        return 1
    try:
        config.install_key(password, host=cfg.host, username=cfg.username,
                           port=cfg.port, key_path=cfg.key)
    except Exception as exc:
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
        for key, status in store.capture_all(screens.SCREENS).items():
            print(f"  {key:<16} {status}")
        print(f"\nHost copy:   {store.host_dir}")
        print(f"Device copy: {store.device_dir}")
    return 0


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
            for key, status in applier.restore_all(screens.SCREENS).items():
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
    return 0


def cmd_screens(args: argparse.Namespace) -> int:
    for s in screens.SCREENS:
        flag = "  [diagnostic]" if s.risky else ""
        print(f"{s.key:<16} {s.width}x{s.height}  {s.label}{flag}")
        print(f"{'':<16} {s.description}")
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("The web UI needs uvicorn: pip install 'rephemeral[ui]'", file=sys.stderr)
        return 1
    from .web import app
    print(f"rePhemeral UI on http://{args.bind}:{args.port}")
    uvicorn.run(app, host=args.bind, port=args.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="rephemeral",
        description="Replace the sleep and restart screens on a reMarkable Paper Pro.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("setup", help="generate a key and install it on the tablet")
    s.add_argument("--host", default=None)
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("status", help="show the device and what is on each screen")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("backup", help="capture the stock screens")
    s.set_defaults(func=cmd_backup)

    s = sub.add_parser("screens", help="list the replaceable screens")
    s.set_defaults(func=cmd_screens)

    s = sub.add_parser("set", help="replace one screen with an image")
    s.add_argument("screen")
    s.add_argument("image")
    s.add_argument("--fit", default="cover", choices=("cover", "contain", "stretch"))
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

    s = sub.add_parser("ui", help="run the local web interface")
    s.add_argument("--bind", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(func=cmd_ui)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except DeviceError as exc:
        print(f"Device error: {exc}", file=sys.stderr)
        return 2
    except KeyError as exc:
        print(exc.args[0] if exc.args else str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
