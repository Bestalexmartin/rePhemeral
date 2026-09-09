"""Capture and restore the device's stock screens.

The design exists to prevent one specific failure. The naive version of
this tool backs up whatever is on the device before each write. Run it
once and set a custom sleep screen; run it again a month later and it
faithfully backs up your custom image over the only copy of the stock
art. The originals are gone, silently, and "restore to stock" quietly
becomes "restore to whatever I had last time".

Three rules prevent that:

1. Backups are write-once per firmware build. An existing entry is never
   overwritten. A new build gets a new directory, because stock art can
   change between firmware releases, and old backups are kept.
2. Every image the tool writes is recorded with its hash. Before
   capturing, a file whose hash the tool recognises as its own is refused
   as a source. The tool will not launder its own output into a backup.
3. Backups are written to the device and to the host. The device copy is
   authoritative for restore, since it travels with the tablet and
   survives the A/B rootfs swap of a firmware update. The host copy is
   the fallback for when the tablet has been wiped, which is not
   hypothetical: enabling developer mode wipes a Paper Pro.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .device import Device
from .screens import Screen

SCHEMA_VERSION = 1

#: On-device backup root, on the encrypted /home partition. That partition
#: is separate from the rootfs, so it survives firmware updates.
DEVICE_BACKUP_ROOT = "/home/root/.rephemeral/backups"

#: Host-side backup root.
HOST_BACKUP_ROOT = Path.home() / ".local" / "share" / "rephemeral" / "backups"

MANIFEST_NAME = "manifest.json"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class BackupError(RuntimeError):
    pass


class StockAlreadyLost(BackupError):
    """Asked to capture stock art, but the device is already customised.

    Raised when the on-device image matches something the tool previously
    wrote and no stock backup exists for this build. There is nothing safe
    to do: capturing would record a custom image as though it were stock.
    """


@dataclass
class ScreenRecord:
    stock_sha256: str | None = None
    captured_at: str | None = None
    backup_file: str | None = None
    applied: list[dict] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "stock_sha256": self.stock_sha256,
            "captured_at": self.captured_at,
            "backup_file": self.backup_file,
            "applied": self.applied,
        }

    @classmethod
    def from_json(cls, d: dict) -> ScreenRecord:
        return cls(
            stock_sha256=d.get("stock_sha256"),
            captured_at=d.get("captured_at"),
            backup_file=d.get("backup_file"),
            applied=list(d.get("applied") or []),
        )


@dataclass
class Manifest:
    build: str
    board: str = ""
    created_at: str = field(default_factory=_now)
    screens: dict[str, ScreenRecord] = field(default_factory=dict)

    def record(self, key: str) -> ScreenRecord:
        return self.screens.setdefault(key, ScreenRecord())

    def is_ours(self, sha: str) -> bool:
        """True if the tool previously wrote an image with this hash."""
        return any(
            entry.get("sha256") == sha
            for rec in self.screens.values()
            for entry in rec.applied
        )

    def to_json(self) -> dict:
        return {
            "schema": SCHEMA_VERSION,
            "build": self.build,
            "board": self.board,
            "created_at": self.created_at,
            "screens": {k: v.to_json() for k, v in self.screens.items()},
        }

    @classmethod
    def from_json(cls, d: dict) -> Manifest:
        m = cls(
            build=d.get("build", ""),
            board=d.get("board", ""),
            created_at=d.get("created_at", _now()),
        )
        m.screens = {
            k: ScreenRecord.from_json(v) for k, v in (d.get("screens") or {}).items()
        }
        return m


class BackupStore:
    """Backups for one firmware build, mirrored on host and device."""

    def __init__(self, device: Device, build: str, board: str = "",
                 host_root: Path | None = None) -> None:
        self.device = device
        self.build = build
        self.board = board
        self.host_dir = (host_root or HOST_BACKUP_ROOT) / build
        self.device_dir = posixpath.join(DEVICE_BACKUP_ROOT, build)
        self.manifest = self._load()

    # -- manifest persistence -----------------------------------------

    @property
    def _host_manifest(self) -> Path:
        return self.host_dir / MANIFEST_NAME

    def _load(self) -> Manifest:
        """Load the manifest, host first, then the device.

        The device copy is the fallback that makes a fresh machine work:
        plug the tablet into a computer that has never seen it and the
        backups are still there, because they live on the tablet's /home.
        Without this the tool would report a fully backed-up device as
        having no backups, and the first write would capture nothing.
        """
        if self._host_manifest.is_file():
            try:
                return Manifest.from_json(json.loads(self._host_manifest.read_text()))
            except (json.JSONDecodeError, OSError) as exc:
                raise BackupError(
                    f"backup manifest at {self._host_manifest} is unreadable: {exc}. "
                    f"Move it aside rather than deleting it; it is the index of "
                    f"your only copy of the stock art."
                ) from exc

        device_manifest = posixpath.join(self.device_dir, MANIFEST_NAME)
        if self.device.exists(device_manifest):
            try:
                raw = self.device.read_bytes(device_manifest)
                manifest = Manifest.from_json(json.loads(raw.decode()))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                raise BackupError(
                    f"the device's backup manifest at {device_manifest} is "
                    f"unreadable: {exc}"
                ) from exc
            # Mirror it back so the host copy exists for next time, and so
            # the fallback survives the tablet being wiped.
            self.host_dir.mkdir(parents=True, exist_ok=True)
            self._host_manifest.write_text(json.dumps(manifest.to_json(), indent=2) + "\n")
            for rec in manifest.screens.values():
                if not rec.backup_file:
                    continue
                local = self.host_dir / rec.backup_file
                if local.is_file():
                    continue
                remote = posixpath.join(self.device_dir, rec.backup_file)
                if self.device.exists(remote):
                    local.write_bytes(self.device.read_bytes(remote))
            return manifest

        return Manifest(build=self.build, board=self.board)

    def save(self) -> None:
        """Persist the manifest to host, then mirror it to the device."""
        self.host_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.manifest.to_json(), indent=2) + "\n"
        self._host_manifest.write_text(payload)
        # /home is already read-write, so no remount is needed here.
        self.device.check(f"mkdir -p '{self.device_dir}'")
        self.device.write_home_bytes(
            posixpath.join(self.device_dir, MANIFEST_NAME), payload.encode()
        )

    # -- capture ------------------------------------------------------

    def needs_capture(self, screen: Screen) -> bool:
        return self.manifest.record(screen.key).stock_sha256 is None

    def capture(self, screen: Screen) -> ScreenRecord:
        """Back up one screen's current image, if not already captured.

        Write-once: an existing record is returned untouched.
        """
        rec = self.manifest.record(screen.key)
        if rec.stock_sha256 is not None:
            return rec

        if not self.device.exists(screen.path):
            raise BackupError(
                f"{screen.path} is not present on this device. Firmware "
                f"{self.build} may name it differently."
            )

        current = self.device.sha256(screen.path)
        if self.manifest.is_ours(current):
            raise StockAlreadyLost(
                f"{screen.key}: the image on the device is one rePhemeral "
                f"wrote, and no stock backup exists for build {self.build}. "
                f"Refusing to record a custom image as stock. Restore this "
                f"screen from another backup, or reflash, before capturing."
            )

        data = self.device.read_bytes(screen.path)
        if hashlib.sha256(data).hexdigest() != current:
            raise BackupError(
                f"{screen.key}: file changed while being read; try again"
            )

        filename = screen.key + ".png"
        self.host_dir.mkdir(parents=True, exist_ok=True)
        (self.host_dir / filename).write_bytes(data)

        self.device.check(f"mkdir -p '{self.device_dir}'")
        self.device.write_home_bytes(
            posixpath.join(self.device_dir, filename), data
        )

        rec.stock_sha256 = current
        rec.captured_at = _now()
        rec.backup_file = filename
        return rec

    def capture_all(self, screens: tuple[Screen, ...]) -> dict[str, str]:
        """Capture every screen. Returns key -> status."""
        results: dict[str, str] = {}
        for s in screens:
            if not self.needs_capture(s):
                results[s.key] = "already backed up"
                continue
            try:
                self.capture(s)
                results[s.key] = "captured"
            except BackupError as exc:
                results[s.key] = f"FAILED: {exc}"
        self.save()
        return results

    # -- applying and restoring ---------------------------------------

    def note_applied(self, screen: Screen, sha: str, source: str) -> None:
        """Record that the tool wrote this image, so it is never mistaken
        for stock art on a later capture."""
        self.manifest.record(screen.key).applied.append(
            {"sha256": sha, "at": _now(), "source": source}
        )

    def stock_bytes(self, screen: Screen) -> bytes:
        """Read the stock image back, device copy first, host as fallback."""
        rec = self.manifest.record(screen.key)
        if rec.stock_sha256 is None or rec.backup_file is None:
            raise BackupError(
                f"no stock backup recorded for {screen.key} on build "
                f"{self.build}; nothing to restore"
            )

        device_path = posixpath.join(self.device_dir, rec.backup_file)
        for source, reader in (
            ("device", lambda: self.device.read_bytes(device_path)),
            ("host", lambda: (self.host_dir / rec.backup_file).read_bytes()),
        ):
            try:
                data = reader()
            except Exception:
                continue
            if hashlib.sha256(data).hexdigest() == rec.stock_sha256:
                return data
            raise BackupError(
                f"{source} backup of {screen.key} does not match its recorded "
                f"hash; it has been modified or corrupted. Refusing to restore it."
            )
        raise BackupError(
            f"stock backup for {screen.key} is missing from both the device "
            f"and {self.host_dir}"
        )

    def drift(self, screens: tuple[Screen, ...]) -> dict[str, str]:
        """Compare the device against the manifest.

        Distinguishes the three states that matter after a firmware update:
        stock, an image the tool applied, and something unrecognised.
        """
        out: dict[str, str] = {}
        for s in screens:
            rec = self.manifest.record(s.key)
            if not self.device.exists(s.path):
                out[s.key] = "missing from device"
                continue
            current = self.device.sha256(s.path)
            if rec.stock_sha256 == current:
                out[s.key] = "stock"
            elif rec.applied and rec.applied[-1].get("sha256") == current:
                out[s.key] = "custom (applied by rePhemeral)"
            elif self.manifest.is_ours(current):
                out[s.key] = "custom (an earlier rePhemeral image)"
            elif rec.stock_sha256 is None:
                out[s.key] = "not backed up"
            else:
                out[s.key] = "unrecognised (firmware update, or changed elsewhere)"
        return out
