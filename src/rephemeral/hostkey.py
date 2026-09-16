"""The tablet's SSH host key, recorded once and checked on every connection.

Through 0.8.0 the tool loaded no known-hosts file at all. Every key was
therefore an unknown key, which the client's AutoAddPolicy accepted, so
anything answering at the configured address was taken to be the tablet:
`setup` handed it the root password before anything checked what it was,
and an ordinary command let it rewrite the host's copy of the stock
artwork, since the backup store trusts the manifest the device serves.

Recording the key closes that, and the policy does not change. paramiko
raises BadHostKeyException by itself once a key is known and differs; the
policy is consulted only for a host it has never seen. What changes here is
that the file exists, is loaded, and is kept beside the private key.

First contact is trusted rather than refused. The tablet regenerates its
host key on a factory reset and developer mode forces one, so refusing an
unknown key would block the tool on the workflow it exists to support, and
would break every install that predates this file. `rephemeral setup
--trust-new-key` is how a re-keyed tablet is trusted again.
"""
from __future__ import annotations

from pathlib import Path

import paramiko

from . import config, paths
from .device import fingerprint

#: Where the recorded key lives, beside the configuration and the private
#: key, so REPHEMERAL_CONFIG_DIR moves all three together. device.py reads
#: the same constant, which is why it is defined in paths.
STORE_PATH = paths.KNOWN_HOSTS

#: Re-exported so callers have one place to ask about host keys, while the
#: refusal message in device.py uses the same form.
__all__ = ["STORE_PATH", "ensure_store", "entry_name", "fingerprint", "forget",
           "record", "recorded", "recorded_fingerprint"]


def entry_name(host: str, port: int = paramiko.config.SSH_PORT) -> str:
    """The name a key is filed under, as paramiko's client writes it.

    It must match exactly, or a recorded key would not be found and every
    connection would look like first contact.
    """
    if port == paramiko.config.SSH_PORT:
        return host
    return f"[{host}]:{port}"


def ensure_store(path: Path | None = None) -> Path:
    """Create the store if it is missing, and keep it private either way.

    It has to exist before connecting: paramiko's load_host_keys sets the
    file it saves to and then reads it, raising if it is not there, and a
    first contact is only recorded when that filename is set.

    The contents are public keys, so the restriction is about who can
    change them rather than who can read them. A writable store is a
    writable answer to "is this the tablet".
    """
    target = path or STORE_PATH
    config.make_private_dir(target.parent)
    if not target.is_file():
        config.write_private(target, b"")
        return target
    # paramiko's save writes the file itself, at whatever the umask allows,
    # so the restriction is reapplied rather than assumed.
    config.restrict_private(target)
    return target


def _keys(path: Path) -> paramiko.HostKeys:
    return paramiko.HostKeys(str(path)) if path.is_file() else paramiko.HostKeys()


def recorded(host: str, port: int = paramiko.config.SSH_PORT,
             path: Path | None = None) -> paramiko.PKey | None:
    """The key recorded for this host, or None if it has never connected."""
    entry = _keys(path or STORE_PATH).lookup(entry_name(host, port))
    if not entry:
        return None
    return next(iter(entry.values()), None)


def recorded_fingerprint(host: str, port: int = paramiko.config.SSH_PORT,
                         path: Path | None = None) -> str | None:
    key = recorded(host, port, path)
    return fingerprint(key) if key is not None else None


def record(host: str, key: paramiko.PKey,
           port: int = paramiko.config.SSH_PORT,
           path: Path | None = None) -> str:
    """Record a key for this host, replacing whatever it has. Returns the
    fingerprint recorded.

    Connecting is what normally records a key. This exists so a failed
    re-trust can put the previous one back, rather than leaving the tablet
    un-pinned because `setup` stopped somewhere in the middle.
    """
    target = ensure_store(path)
    keys = _keys(target)
    name = entry_name(host, port)
    if keys.lookup(name):
        del keys[name]
    keys.add(name, key.get_name(), key)
    keys.save(str(target))
    config.restrict_private(target)
    return fingerprint(key)


def forget(host: str, port: int = paramiko.config.SSH_PORT,
           path: Path | None = None) -> str | None:
    """Drop this host's recorded key. Returns the fingerprint it dropped.

    Only `setup --trust-new-key` calls this. Nothing else replaces a
    recorded key, because a key that changes silently is the thing this
    module exists to notice.
    """
    target = path or STORE_PATH
    if not target.is_file():
        return None
    keys = _keys(target)
    name = entry_name(host, port)
    entry = keys.lookup(name)
    if not entry:
        return None
    dropped = next(iter(entry.values()), None)
    del keys[name]
    keys.save(str(target))
    config.restrict_private(target)
    return fingerprint(dropped) if dropped is not None else None
