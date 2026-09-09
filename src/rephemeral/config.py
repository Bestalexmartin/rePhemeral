"""Configuration and SSH key management.

The tablet's root password is used exactly once, to install a keypair, and
is never written to disk. Everything afterwards is key authentication.
That is not just hygiene: the password is printed in the tablet's settings
UI and regenerates on a factory reset, so treating it as a stored
credential would be wrong as well as unsafe.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

CONFIG_DIR = Path(
    os.environ.get("REPHEMERAL_CONFIG_DIR", Path.home() / ".config" / "rephemeral")
)
CONFIG_PATH = CONFIG_DIR / "rephemeral.toml"
KEY_PATH = CONFIG_DIR / "id_ed25519"

DEFAULT_HOST = "10.11.99.1"
DEFAULT_USER = "root"


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
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "# rePhemeral configuration.\n"
            "# The device password is deliberately absent: it is used once to\n"
            "# install the key below, then discarded. Never add it here.\n"
            f'host = "{self.host}"\n'
            f'username = "{self.username}"\n'
            f"port = {self.port}\n"
            f'key_path = "{self.key}"\n'
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


def ensure_key(key_path: Path | None = None) -> Path:
    """Generate the tool's keypair if it does not exist. Returns its path."""
    target = key_path or KEY_PATH
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    # paramiko can load ed25519 keys but cannot generate them, so the
    # keypair is produced with cryptography (already a paramiko dependency)
    # and written in OpenSSH format for both paramiko and ssh(1) to read.
    private = ed25519.Ed25519PrivateKey.generate()
    target.write_bytes(
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    target.chmod(0o600)
    pub_line = private.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode()
    pub = target.with_suffix(target.suffix + ".pub")
    pub.write_text(f"{pub_line} rephemeral\n")
    pub.chmod(0o644)
    return target


def public_key_line(key_path: Path | None = None) -> str:
    target = key_path or KEY_PATH
    pub = target.with_suffix(target.suffix + ".pub")
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

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host, port=port, username=username, password=password,
            timeout=10, allow_agent=False, look_for_keys=False,
        )
        # Append only if absent, so re-running is harmless.
        cmd = (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f"grep -qxF {_q(line)} ~/.ssh/authorized_keys 2>/dev/null || "
            f"echo {_q(line)} >> ~/.ssh/authorized_keys; "
            "chmod 600 ~/.ssh/authorized_keys"
        )
        _in, out, err = client.exec_command(cmd, timeout=10)
        rc = out.channel.recv_exit_status()
        if rc != 0:
            raise RuntimeError(err.read().decode("utf-8", "replace").strip())
    finally:
        client.close()


def remove_key(device, key_line: str | None = None) -> None:
    """Remove the tool's key from the tablet's authorized_keys."""
    line = key_line or public_key_line()
    # Match on the key body, since the trailing comment may differ.
    body = line.split()[1] if len(line.split()) > 1 else line
    device.check(
        f"test -f ~/.ssh/authorized_keys && "
        f"grep -v {_q(body)} ~/.ssh/authorized_keys > ~/.ssh/.ak.tmp && "
        f"mv ~/.ssh/.ak.tmp ~/.ssh/authorized_keys || true"
    )


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
