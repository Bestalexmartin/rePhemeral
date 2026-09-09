"""SSH transport to a reMarkable Paper Pro over USB.

The device presents a USB ethernet interface, serves DHCP, and answers on
10.11.99.1. Its SSH daemon is dropbear, run from inetd, with SFTP provided
by /usr/libexec/sftp-server.

The rootfs is ext4 mounted read-only with no dm-verity, so writing to it is
a matter of remounting rw and back. That remount is the single most
dangerous thing this tool does: leaving a tablet's rootfs writable invites
corruption on an unclean shutdown, which for a device that boots from that
partition is unrecoverable without a reflash. Every write therefore goes
through `writable_rootfs`, which reverts in a `finally` and verifies the
revert actually took.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import posixpath
import stat as statmod
from collections.abc import Iterator
from dataclasses import dataclass
from shlex import quote as _q
from uuid import uuid4

import paramiko

DEFAULT_HOST = "10.11.99.1"
DEFAULT_USER = "root"
DEFAULT_PORT = 22

#: Refuse to write if the rootfs would drop below this much free space.
#: Measured free space on a stock 3.28 device is around 41 MB, so this is a
#: meaningful floor rather than a formality. Filling the rootfs of a device
#: that boots from it is the worst outcome this tool could produce.
MIN_FREE_BYTES = 8 * 1024 * 1024

#: Reject any single image larger than this. Stock screens are 20-190 KB;
#: a photographic RGBA PNG at panel size can be several MB and a handful
#: would exhaust the partition.
MAX_IMAGE_BYTES = 4 * 1024 * 1024


class DeviceError(RuntimeError):
    """Any failure talking to the tablet."""


class NotAReMarkableError(DeviceError):
    """The host answered, but does not look like a reMarkable."""


class InsufficientSpaceError(DeviceError):
    """Writing would leave the rootfs dangerously full."""


class SymlinkRefused(DeviceError):
    """The target path is a symlink; writing would clobber its target."""


@dataclass(frozen=True)
class DeviceInfo:
    build: str
    """Contents of /etc/version, e.g. 20260827113527. Identifies the
    firmware build, and keys the backup set."""

    kernel: str
    board: str
    free_bytes: int
    total_bytes: int

    @property
    def free_mb(self) -> float:
        return self.free_bytes / (1024 * 1024)


class Device:
    """A connected tablet. Use as a context manager."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        key_path: str | None = None,
        username: str = DEFAULT_USER,
        port: int = DEFAULT_PORT,
        password: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.key_path = key_path
        self._password = password
        self.timeout = timeout
        self._client: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None
        # Depth counter so nested writable_rootfs() blocks do not remount
        # read-only while an outer block is still writing.
        self._rw_depth = 0

    # -- lifecycle ----------------------------------------------------

    def connect(self) -> None:
        self.close()
        client = paramiko.SSHClient()
        self._client = client
        # The tablet regenerates its host key on a factory reset, and
        # developer mode forces one. Rejecting the new key would block the
        # tool on exactly the workflow it exists to support, so we accept
        # it. The link is a direct USB cable, not a network path, which is
        # what makes that acceptable here and would not elsewhere.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                key_filename=self.key_path,
                password=self._password,
                timeout=self.timeout,
                banner_timeout=self.timeout,
                auth_timeout=self.timeout,
                channel_timeout=self.timeout,
                allow_agent=False,
                look_for_keys=False,
            )
            self._sftp = client.open_sftp()
            self._sftp.get_channel().settimeout(self.timeout)
            self._assert_is_remarkable()
        except DeviceError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise DeviceError(f"could not connect to {self.host}: {exc}") from exc
        finally:
            self._password = None

    def close(self) -> None:
        # If an exception escapes mid-write the rootfs could still be rw.
        # Make a best-effort revert before dropping the connection.
        if self._rw_depth > 0:
            self._rw_depth = 0
            with contextlib.suppress(Exception):
                self.run("sync; mount -o remount,ro /")
        with contextlib.suppress(Exception):
            if self._sftp is not None:
                self._sftp.close()
        with contextlib.suppress(Exception):
            if self._client is not None:
                self._client.close()
        self._sftp = None
        self._client = None

    def __enter__(self) -> Device:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- primitives ---------------------------------------------------

    def run(self, command: str) -> tuple[int, str, str]:
        """Run a shell command. Returns (exit_status, stdout, stderr)."""
        if self._client is None:
            raise DeviceError("not connected")
        channel = None
        try:
            _in, out, err = self._client.exec_command(command, timeout=self.timeout)
            channel = out.channel
            stdout = out.read().decode("utf-8", "replace")
            stderr = err.read().decode("utf-8", "replace")
            return channel.recv_exit_status(), stdout, stderr
        except (OSError, paramiko.SSHException) as exc:
            raise DeviceError(f"connection to {self.host} failed: {exc}") from exc
        finally:
            if channel is not None:
                channel.close()

    def check(self, command: str) -> str:
        """Run a command, raising on non-zero exit. Returns stripped stdout."""
        rc, out, err = self.run(command)
        if rc != 0:
            raise DeviceError(f"`{command}` failed ({rc}): {err.strip() or out.strip()}")
        return out.strip()

    def _assert_is_remarkable(self) -> None:
        """Refuse to touch a host that is not a reMarkable.

        This tool remounts a root filesystem read-write and overwrites
        files in /usr/share. Pointed at the wrong machine by a stale
        config or a typo'd address, that is straightforwardly destructive,
        so identity is established before anything else happens.
        """
        rc, out, _ = self.run("test -d /usr/share/remarkable && uname -n")
        if rc != 0 or not out.strip():
            raise NotAReMarkableError(
                f"{self.host} does not have /usr/share/remarkable; refusing to continue"
            )

    # -- inspection ---------------------------------------------------

    def info(self) -> DeviceInfo:
        build = self.check("cat /etc/version")
        kernel = self.check("uname -r")
        board = self.check("uname -n")
        free, total = self._rootfs_space()
        return DeviceInfo(
            build=build, kernel=kernel, board=board,
            free_bytes=free, total_bytes=total,
        )

    def _rootfs_space(self) -> tuple[int, int]:
        """Free and total bytes on /, via df in 1K blocks."""
        out = self.check("df -k / | tail -n 1")
        parts = out.split()
        # Filesystem 1K-blocks Used Available Use% Mounted
        try:
            total_k, _used_k, avail_k = int(parts[1]), int(parts[2]), int(parts[3])
        except (IndexError, ValueError) as exc:
            raise DeviceError(f"could not parse df output: {out!r}") from exc
        return avail_k * 1024, total_k * 1024

    def sha256(self, path: str) -> str:
        """Hash a file on the device, without transferring it."""
        out = self.check(f"sha256sum {_q(path)}")
        return out.split()[0]

    def exists(self, path: str) -> bool:
        rc, _, _ = self.run(f"test -e {_q(path)}")
        return rc == 0

    def is_symlink(self, path: str) -> bool:
        """True if path is a symlink, without following it.

        restart-crashed.png ships as a symlink to rebooting.png. Writing
        through it would silently overwrite the reboot screen instead, so
        callers must check.
        """
        if self._sftp is None:
            raise DeviceError("not connected")
        try:
            return statmod.S_ISLNK(self._sftp.lstat(path).st_mode)
        except FileNotFoundError:
            return False

    def readlink(self, path: str) -> str | None:
        rc, out, _ = self.run(f"readlink {_q(path)}")
        return out.strip() if rc == 0 and out.strip() else None

    # -- file transfer ------------------------------------------------

    def read_bytes(self, path: str) -> bytes:
        if self._sftp is None:
            raise DeviceError("not connected")
        buf = io.BytesIO()
        self._sftp.getfo(path, buf)
        return buf.getvalue()

    def write_bytes(self, path: str, data: bytes, mode: int = 0o644) -> str:
        """Write a file to the rootfs, returning the resulting sha256.

        Must be called inside a `writable_rootfs` block. Refuses to follow
        symlinks and refuses to fill the partition. Writes to a temporary
        file in the same directory and renames over the target, so an
        interrupted transfer cannot leave a half-written screen in place.
        """
        if self._sftp is None:
            raise DeviceError("not connected")
        if self._rw_depth <= 0:
            raise DeviceError(
                "write_bytes called outside writable_rootfs(); refusing"
            )
        if len(data) > MAX_IMAGE_BYTES:
            raise InsufficientSpaceError(
                f"{len(data) / 1024 / 1024:.1f} MB exceeds the "
                f"{MAX_IMAGE_BYTES / 1024 / 1024:.0f} MB per-image cap"
            )
        if self.is_symlink(path):
            target = self.readlink(path)
            raise SymlinkRefused(
                f"{path} is a symlink to {target!r}; writing through it would "
                f"overwrite that file instead. Write to the target directly "
                f"if that is what you meant."
            )

        free, _total = self._rootfs_space()
        # The old file remains allocated until the temporary upload is renamed.
        projected = free - len(data)
        if projected < MIN_FREE_BYTES:
            raise InsufficientSpaceError(
                f"writing {len(data) // 1024} KB would leave "
                f"{projected // 1024} KB free on /, below the "
                f"{MIN_FREE_BYTES // 1024} KB floor. The rootfs is where the "
                f"tablet boots from; filling it is not recoverable over SSH."
            )

        tmp = posixpath.join(posixpath.dirname(path), f".rephemeral.{uuid4().hex}.tmp")
        try:
            self._sftp.putfo(io.BytesIO(data), tmp, confirm=True)
            self._sftp.chmod(tmp, mode)
            # Atomic within the filesystem: either the old file or the new.
            self.check(f"mv -f {_q(tmp)} {_q(path)}")
        except Exception:
            with contextlib.suppress(Exception):
                self.run(f"rm -f {_q(tmp)}")
            raise
        self.check("sync")

        written = self.sha256(path)
        expected = hashlib.sha256(data).hexdigest()
        if written != expected:
            raise DeviceError(
                f"verification failed for {path}: device reports {written}, "
                f"expected {expected}"
            )
        return written

    def write_home_bytes(self, path: str, data: bytes, mode: int = 0o600) -> str:
        """Write a file under /home, which is already read-write.

        /home is a separate encrypted partition, so this needs no remount
        and is not subject to the rootfs space floor. Backups live here.
        Deliberately a different method from `write_bytes` so that no
        backup write can accidentally travel through the rootfs guard, and
        so that a caller cannot use it to reach /usr.
        """
        if self._sftp is None:
            raise DeviceError("not connected")
        path = posixpath.normpath(path)
        if not path.startswith("/home/"):
            raise DeviceError(
                f"write_home_bytes refuses {path!r}: only paths under /home"
            )
        tmp = f"{path}.{uuid4().hex}.tmp"
        try:
            self._sftp.putfo(io.BytesIO(data), tmp, confirm=True)
            self._sftp.chmod(tmp, mode)
            self.check(f"mv -f {_q(tmp)} {_q(path)}")
        except Exception:
            with contextlib.suppress(Exception):
                self.run(f"rm -f {_q(tmp)}")
            raise
        written = self.sha256(path)
        expected = hashlib.sha256(data).hexdigest()
        if written != expected:
            raise DeviceError(
                f"verification failed for {path}: device reports {written}, "
                f"expected {expected}"
            )
        return written

    # -- the dangerous part -------------------------------------------

    @contextlib.contextmanager
    def writable_rootfs(self) -> Iterator[None]:
        """Remount / read-write for the duration of the block.

        Reverts to read-only in a finally, and raises if the revert fails,
        because a tablet left with a writable rootfs is a tablet one
        unclean shutdown away from a reflash. Re-entrant: only the
        outermost block performs the remount.
        """
        if self._rw_depth > 0:
            self._rw_depth += 1
            try:
                yield
            finally:
                self._rw_depth -= 1
            return

        rc, _, err = self.run("mount -o remount,rw /")
        if rc != 0:
            raise DeviceError(
                f"could not remount / read-write: {err.strip()}. On firmware "
                f"with a verified rootfs this is expected and the tool cannot "
                f"proceed."
            )
        self._rw_depth = 1
        try:
            yield
        finally:
            rc, _, err = self.run("sync; mount -o remount,ro /")
            if rc != 0:
                raise DeviceError(
                    "FAILED TO REMOUNT / READ-ONLY. The tablet's root "
                    "filesystem is still writable. Reboot it before "
                    f"disconnecting: `ssh root@{self.host} reboot`. "
                    f"Cause: {err.strip()}"
                )
            options = self.check("awk '$2 == \"/\" {print $4}' /proc/mounts")
            if "ro" not in options.split(",") or "rw" in options.split(","):
                raise DeviceError(
                    "/ still reports read-write after remount; reboot the "
                    "tablet before disconnecting."
                )

            self._rw_depth = 0
