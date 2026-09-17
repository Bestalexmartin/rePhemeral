"""Configuration and SSH key management.

The tablet's root password is used exactly once, to install a keypair, and
is never written to disk. Everything afterwards is key authentication.
That is not just hygiene: the password is printed in the tablet's settings
UI and regenerates on a factory reset, so treating it as a stored
credential would be wrong as well as unsafe.
"""
from __future__ import annotations

import json
import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from shlex import quote as _q

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from . import paths
from .device import DEFAULT_HOST, DEFAULT_USER, Device

CONFIG_NAME = "rephemeral.toml"
KEY_NAME = "id_ed25519"

CONFIG_DIR = paths.CONFIG_DIR
CONFIG_PATH = CONFIG_DIR / CONFIG_NAME
KEY_PATH = CONFIG_DIR / KEY_NAME

#: Who a private file is left readable by, for messages.
PRIVATE_READERS = "its owner and SYSTEM" if os.name == "nt" else "its owner"


@dataclass
class Config:
    host: str = DEFAULT_HOST
    username: str = DEFAULT_USER
    port: int = 22
    key_path: str = ""

    @property
    def key(self) -> Path:
        return Path(self.key_path) if self.key_path else KEY_PATH

    def save(self, path: Path | None = None) -> Path:
        target = path or CONFIG_PATH
        make_private_dir(target.parent)
        target.write_text(
            "# rePhemeral configuration.\n"
            "# The device password is deliberately absent: it is used once to\n"
            "# install the key below, then discarded. Never add it here.\n"
            f"host = {json.dumps(self.host, ensure_ascii=False)}\n"
            f"username = {json.dumps(self.username, ensure_ascii=False)}\n"
            f"port = {self.port}\n"
            f"key_path = {json.dumps(str(self.key), ensure_ascii=False)}\n"
        )
        target.chmod(0o600)
        return target


def load(path: Path | None = None) -> Config:
    target = path or CONFIG_PATH
    if not target.is_file():
        return Config(key_path=str(KEY_PATH))
    data = tomllib.loads(target.read_text())
    return Config(
        host=data.get("host", DEFAULT_HOST),
        username=data.get("username", DEFAULT_USER),
        port=int(data.get("port", 22)),
        key_path=data.get("key_path") or str(KEY_PATH),
    )


# -- keeping the key private --------------------------------------------

def restrict_private(path: Path) -> None:
    """Make path accessible to its owner alone.

    POSIX expresses that with a mode. Windows cannot: chmod there only
    toggles the read-only attribute and os.open ignores its mode argument,
    so a file keeps whatever ACL it inherits, and neither call raises. The
    platforms differ in kind here, which is why this branches on os.name.
    On Windows SYSTEM keeps access alongside the owner; see winacl.restrict.
    """
    if os.name == "nt":
        from . import winacl
        winacl.restrict(path)
    else:
        path.chmod(0o700 if path.is_dir() else 0o600)


def key_exposure(path: Path) -> list[str]:
    """Who, besides PRIVATE_READERS, can read path. Empty when it is private."""
    if os.name == "nt":
        from . import winacl
        return [name for _sid, name in winacl.other_readers(path)]
    mode = stat.S_IMODE(path.stat().st_mode)
    return [who for who, bits in (("its group", 0o070), ("other users", 0o007))
            if mode & bits]


#: Windows gives BUILTIN\\Administrators full control of nearly every folder
#: under a profile, by inheritance. Counting that as an exposure would warn
#: almost every older install about the ordinary state of the machine, and
#: a warning that is always on is a warning nobody reads. An administrator
#: can take ownership of anything regardless, so the warning would also be
#: telling people about a boundary that was never there. `winacl.restrict`
#: still leaves Administrators out of any ACL it writes; they are simply not
#: a reason to warn.
ADMINISTRATORS_SID = "S-1-5-32-544"


def notable_writers(writers: list[tuple[str, str]],
                    include_administrators: bool = False) -> list[str]:
    """The names worth reporting, from (SID, name) pairs.

    Kept separate from the ACL reading so the rule can be tested anywhere,
    rather than only on the platform that produces such pairs.
    """
    return [name for sid, name in writers
            if include_administrators or sid != ADMINISTRATORS_SID]


def dir_exposure(path: Path, include_administrators: bool = False) -> list[str]:
    """Who else can write into path. Empty when only its owner can.

    A private key inside a folder others can write to is not much of a
    secret: they cannot read it, but they can replace it, and the config
    beside it that says which host to hand it to. Installs made before
    this tool created its own folder have exactly that shape, a 775
    directory holding a 600 key.

    Windows Administrators are ignored unless asked for; see
    ADMINISTRATORS_SID.
    """
    if os.name == "nt":
        from . import winacl
        return notable_writers(winacl.other_writers(path), include_administrators)
    mode = stat.S_IMODE(path.stat().st_mode)
    return [who for who, bits in (("its group", 0o020), ("other users", 0o002))
            if mode & bits]


def make_private_dir(path: Path) -> None:
    """Create path if it is missing, restricted to its owner.

    Files written into a directory created here inherit that restriction.
    An existing directory is left as it is, since it may hold more than
    this tool's files. `restrict_existing_dir` is the deliberate exception.
    """
    if path.is_dir():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()
    restrict_private(path)


def restrict_existing_dir(path: Path) -> list[str]:
    """Restrict a directory the tool owns, even if it was already there.

    Returns whoever could write into it beforehand, so the caller can say
    what it changed. Callers must only point this at the tool's own
    location: a directory someone chose with REPHEMERAL_CONFIG_DIR may be
    shared on purpose, and this would quietly lock their collaborators out.
    """
    if not path.is_dir():
        return []
    exposed = dir_exposure(path)
    if exposed:
        restrict_private(path)
    return exposed


def write_private(path: Path, data: bytes) -> None:
    """Write a new file that only its owner can read.

    The file is created empty and restricted before any of data reaches
    it, so there is no moment at which the secret sits on disk readable
    by anyone else. Refuses to overwrite an existing file.
    """
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    try:
        restrict_private(path)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    # r+b opens the file that was just restricted, rather than replacing it.
    with open(path, "r+b") as stream:
        stream.write(data)


def pub_path(key_path: Path) -> Path:
    return key_path.with_suffix(key_path.suffix + ".pub")


def ensure_key(key_path: Path | None = None) -> Path:
    """Generate the tool's keypair if it does not exist, and make sure the
    private half is restricted either way. Returns its path."""
    target = key_path or KEY_PATH
    if target.is_file():
        # An install made before the tool restricted keys on Windows, or a
        # key whose permissions were loosened since, is fixed here.
        restrict_private(target)
        return target
    make_private_dir(target.parent)
    # paramiko can load ed25519 keys but cannot generate them, so the
    # keypair is produced with cryptography (already a paramiko dependency)
    # and written in OpenSSH format for both paramiko and ssh(1) to read.
    private = ed25519.Ed25519PrivateKey.generate()
    write_private(target, private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    pub_line = private.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode()
    pub = pub_path(target)
    pub.write_text(f"{pub_line} rephemeral\n")
    pub.chmod(0o644)
    return target


def public_key_line(key_path: Path | None = None) -> str:
    target = key_path or KEY_PATH
    pub = pub_path(target)
    if pub.is_file():
        return pub.read_text().strip()
    key = paramiko.Ed25519Key.from_private_key_file(str(target))
    return f"{key.get_name()} {key.get_base64()} rephemeral"


def install_key(
    password: str,
    host: str = DEFAULT_HOST,
    username: str = DEFAULT_USER,
    port: int = 22,
    key_path: Path | None = None,
) -> None:
    """Install the tool's public key on the tablet using the root password.

    The password is a parameter and a local; it is never persisted. Callers
    should read it from a prompt, pass it here, and let it go out of scope.
    """
    target = ensure_key(key_path)
    line = public_key_line(target)

    with Device(host=host, username=username, port=port, password=password) as device:
        # Append only if absent, so re-running is harmless.
        device.check(
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f"(grep -qxF {_q(line)} ~/.ssh/authorized_keys 2>/dev/null || "
            f"echo {_q(line)} >> ~/.ssh/authorized_keys) && "
            "chmod 600 ~/.ssh/authorized_keys"
        )


def remove_key(device, key_line: str | None = None) -> None:
    """Remove the tool's key from the tablet's authorized_keys."""
    line = key_line or public_key_line()
    # Match on the key body, since the trailing comment may differ.
    body = line.split()[1] if len(line.split()) > 1 else line
    device.check(
        "if test -f ~/.ssh/authorized_keys; then "
        "umask 077; "
        f"awk -v key={_q(body)} '$2 != key' ~/.ssh/authorized_keys > ~/.ssh/.ak.tmp && "
        "chmod 600 ~/.ssh/.ak.tmp && mv ~/.ssh/.ak.tmp ~/.ssh/authorized_keys; fi"
    )
