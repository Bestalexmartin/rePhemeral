"""Where rePhemeral keeps its files on this computer.

macOS and Linux keep the layout the tool has always used: configuration
and the SSH key in ~/.config/rephemeral, and backups in
~/.local/share/rephemeral/backups.

Windows keeps both in %LOCALAPPDATA%\\rephemeral, with backups in a
subfolder. The platforms differ in kind here rather than in detail:
Windows has its own convention for per-user application data, and a
dotfolder at the root of the profile is not where a Windows user, or
another tool, would look. LOCALAPPDATA rather than APPDATA because roaming
profiles copy APPDATA between machines, and neither a private key nor
backups belonging to one tablet should travel that way.

Either location can be overridden. REPHEMERAL_CONFIG_DIR sets where
configuration and the key go. REPHEMERAL_DATA_DIR sets the data
directory, with backups in its backups subfolder.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def legacy_dirs(home: Path | None = None) -> tuple[Path, Path]:
    """The configuration and data directories every platform used before
    Windows had its own. Still current on macOS and Linux."""
    home = home or Path.home()
    return home / ".config" / "rephemeral", home / ".local" / "share" / "rephemeral"


def default_dirs(
    os_name: str = os.name,
    environ: Mapping[str, str] = os.environ,
    home: Path | None = None,
) -> tuple[Path, Path]:
    """The configuration and data directories, after any override."""
    home = home or Path.home()
    if os_name == "nt":
        base = Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "rephemeral"
        config_dir, data_dir = base, base
    else:
        config_dir, data_dir = legacy_dirs(home)
    return (
        Path(environ.get("REPHEMERAL_CONFIG_DIR") or config_dir),
        Path(environ.get("REPHEMERAL_DATA_DIR") or data_dir),
    )


CONFIG_DIR, DATA_DIR = default_dirs()
BACKUP_DIR = DATA_DIR / "backups"

#: The tablet's recorded SSH host key. It lives beside the configuration and
#: the private key so one override moves all three, and it is named here
#: rather than in hostkey.py because device.py needs it too and cannot
#: import that module: hostkey imports config, and config imports device.
KNOWN_HOSTS = CONFIG_DIR / "known_hosts"
