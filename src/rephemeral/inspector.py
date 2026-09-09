"""Environment and connectivity diagnosis.

Exists because this tool spans four layers that fail differently: the
Python environment, the host operating system's handling of a USB
ethernet device, SSH authentication, and the tablet itself. A bare
"could not connect to 10.11.99.1" does not say which of those broke, and
on a machine that is not the one the tool was developed on, that is the
only question worth answering.

Every check is read-only and none of them writes to the tablet.
"""
from __future__ import annotations

import os
import socket
import stat
import sys
from dataclasses import dataclass

from . import config, screens

OK, WARN, FAIL, SKIP = "ok", "warn", "FAIL", "skip"


@dataclass
class Check:
    name: str
    status: str
    detail: str


def _python() -> Check:
    v = sys.version_info
    got = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) < (3, 11):
        return Check("Python", FAIL,
                     f"{got}; 3.11 or newer required (config parsing uses tomllib)")
    return Check("Python", OK, f"{got} at {sys.executable}")


def _platform() -> Check:
    detail = f"{sys.platform}, os.name={os.name}"
    if os.name == "nt":
        detail += "; POSIX file modes do not apply, see the SSH key check"
    return Check("Platform", OK, detail)


def _deps() -> Check:
    missing, found = [], []
    for mod, label in (("paramiko", "paramiko"), ("PIL", "Pillow")):
        try:
            m = __import__(mod)
            found.append(f"{label} {getattr(m, '__version__', '?')}")
        except ImportError:
            missing.append(label)
    if missing:
        return Check("Dependencies", FAIL, f"missing: {', '.join(missing)}")
    return Check("Dependencies", OK, ", ".join(found))


def _config(cfg: config.Config) -> Check:
    if config.CONFIG_PATH.is_file():
        return Check("Config", OK, f"{config.CONFIG_PATH} -> {cfg.host}:{cfg.port}")
    return Check("Config", WARN,
                 f"none at {config.CONFIG_PATH}; using defaults "
                 f"({cfg.host}:{cfg.port}). Run `rephemeral setup`.")


def _key(cfg: config.Config) -> Check:
    if not cfg.key.is_file():
        return Check("SSH key", FAIL,
                     f"none at {cfg.key}; run `rephemeral setup`")
    if os.name == "nt":
        return Check("SSH key", WARN,
                     f"{cfg.key} exists. Windows cannot express POSIX mode 600, "
                     f"so this key may be readable by other accounts on this "
                     f"machine. Restrict it with ACLs on a shared computer.")
    mode = stat.S_IMODE(cfg.key.stat().st_mode)
    if mode & 0o077:
        return Check("SSH key", WARN,
                     f"{cfg.key} is mode {mode:03o}; group or others can read it. "
                     f"Fix with: chmod 600 {cfg.key}")
    return Check("SSH key", OK, f"{cfg.key}, mode {mode:03o}")


def _route(cfg: config.Config) -> Check:
    """Which local address the OS would use to reach the tablet.

    Reveals whether the USB ethernet interface came up, without shelling
    out to ifconfig or ip, neither of which exists everywhere. A UDP
    socket's connect() only sets the peer and picks a route; it sends
    nothing.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((cfg.host, 9))
        local = s.getsockname()[0]
    except OSError as exc:
        return Check("USB network", FAIL,
                     f"no route to {cfg.host} ({exc.strerror or exc}). The tablet's "
                     f"USB ethernet interface is not up. Check the cable, and that "
                     f"the OS bound a driver to the device.")
    finally:
        s.close()
    expected = cfg.host.rsplit(".", 1)[0]
    if local.startswith(expected.rsplit(".", 1)[0]):
        return Check("USB network", OK, f"local address {local} reaches {cfg.host}")
    return Check("USB network", WARN,
                 f"{cfg.host} routes via {local}, which is not on the tablet's "
                 f"subnet. Something else may be answering on that address.")


def _tcp(cfg: config.Config) -> Check:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2.0)
    try:
        s.connect((cfg.host, cfg.port))
        return Check("Port open", OK, f"{cfg.host}:{cfg.port} accepts connections")
    except OSError as exc:
        return Check("Port open", FAIL,
                     f"{cfg.host}:{cfg.port} refused or timed out ({exc.strerror or exc})")
    finally:
        s.close()


def _ssh(cfg: config.Config) -> tuple[Check, object | None]:
    """Authenticate and identify the device. Returns the open Device, or None."""
    from .device import Device, DeviceError, NotAReMarkableError
    if not cfg.key.is_file():
        return Check("SSH", SKIP, "no key to authenticate with"), None
    d = Device(host=cfg.host, key_path=str(cfg.key), username=cfg.username,
               port=cfg.port, timeout=8.0)
    try:
        d.connect()
    except NotAReMarkableError as exc:
        return Check("SSH", FAIL, str(exc)), None
    except DeviceError as exc:
        msg = str(exc)
        if "reset" in msg.lower() or "banner" in msg.lower():
            msg += (". A Paper Pro accepts the connection and resets it until "
                    "Developer Mode is enabled. Enabling it erases the device.")
        return Check("SSH", FAIL, msg), None
    return Check("SSH", OK, f"authenticated as {cfg.username}@{cfg.host}"), d


def run(cfg: config.Config | None = None) -> list[Check]:
    cfg = cfg or config.load()
    checks = [_python(), _platform(), _deps(), _config(cfg), _key(cfg)]

    route = _route(cfg)
    checks.append(route)
    if route.status == FAIL:
        checks.append(Check("Port open", SKIP, "no route to the tablet"))
        checks.append(Check("SSH", SKIP, "no route to the tablet"))
        checks.append(Check("Device", SKIP, "no route to the tablet"))
        checks.append(Check("Screens", SKIP, "no route to the tablet"))
        checks.append(Check("Backups", SKIP, "no route to the tablet"))
        return checks

    tcp = _tcp(cfg)
    checks.append(tcp)
    if tcp.status == FAIL:
        for n in ("SSH", "Device", "Screens", "Backups"):
            checks.append(Check(n, SKIP, "nothing listening"))
        return checks

    ssh, device = _ssh(cfg)
    checks.append(ssh)
    if device is None:
        for n in ("Device", "Screens", "Backups"):
            checks.append(Check(n, SKIP, "no SSH session"))
        return checks

    try:
        info = device.info()
        low = info.free_mb < 12
        checks.append(Check("Device", WARN if low else OK,
                            f"{info.board}, build {info.build}, "
                            f"{info.free_mb:.1f} MB free on /"
                            + (" (low; writes may be refused)" if low else "")))

        missing = [s.key for s in screens.SCREENS if not device.exists(s.path)]
        if missing:
            checks.append(Check("Screens", WARN,
                                f"{len(screens.SCREENS) - len(missing)} of "
                                f"{len(screens.SCREENS)} present; missing: "
                                f"{', '.join(missing)}. This firmware may name "
                                f"them differently."))
        else:
            checks.append(Check("Screens", OK,
                                f"all {len(screens.SCREENS)} present at their "
                                f"expected paths"))

        from .backup import BackupStore
        store = BackupStore(device, build=info.build, board=info.board)
        have = sum(1 for s in screens.SCREENS
                   if store.manifest.record(s.key).stock_sha256)
        checks.append(Check("Backups", OK if have == len(screens.SCREENS) else WARN,
                            f"{have} of {len(screens.SCREENS)} screens backed up "
                            f"for build {info.build}; host copy at {store.host_dir}"))
    finally:
        device.close()
    return checks
