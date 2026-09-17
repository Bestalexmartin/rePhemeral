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
   capturing, a file whose hash the tool recognizes as its own is refused
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
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from shlex import quote

from . import paths
from .device import Device
from .screens import Screen

SCHEMA_VERSION = 1

#: On-device backup root, on the encrypted /home partition. That partition
#: is separate from the rootfs, so it survives firmware updates.
DEVICE_BACKUP_ROOT = "/home/root/.rephemeral/backups"

#: Host-side backup root. See paths for where that is on each platform.
HOST_BACKUP_ROOT = paths.BACKUP_DIR

MANIFEST_NAME = "manifest.json"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class BackupError(RuntimeError):
    pass


def _ago(seconds: int) -> str:
    """A rough span, for saying how long after a build a file was written."""
    days, rest = divmod(max(seconds, 0), 86400)
    hours = rest // 3600
    if days and hours:
        return f"{days} day{'s' if days != 1 else ''} and {hours} hour{'s' if hours != 1 else ''}"
    if days:
        return f"{days} day{'s' if days != 1 else ''}"
    if hours:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{rest // 60} minutes"


class StockLooksModified(BackupError):
    """Asked to capture a screen written after the firmware was laid down.

    The firmware stamps its files with the build time, so a later mtime
    means something wrote that screen afterwards. Normally the manifest
    settles whether that something was this tool, but when a tablet's
    /home has been erased and this computer has never seen the tablet
    before, there is no manifest to consult and `is_ours` has nothing to
    compare against. A factory reset erases /home and leaves the rootfs
    alone, so exactly that combination puts custom art in front of a tool
    with no memory of it.

    Refusing is the safe answer: capturing would record someone's custom
    image as the only surviving copy of the stock art. Restoring a screen
    also updates its mtime, so a genuinely stock screen can be refused
    this way; `assume_stock` is the way past it.
    """


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
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", build):
            raise BackupError(f"invalid firmware build: {build!r}")
        self.device = device
        self.build = build
        self.board = board
        self.host_dir = (host_root or HOST_BACKUP_ROOT) / build
        self.device_dir = posixpath.join(DEVICE_BACKUP_ROOT, build)
        self.manifest = self._load()
        #: True when nothing on this computer or the tablet knew anything
        #: about this build when the store opened. Recorded once, here,
        #: because capturing the first screen makes the manifest non-empty
        #: and the question would otherwise answer itself differently for
        #: every screen after it. See StockLooksModified.
        self.arrived_empty = not any(
            rec.stock_sha256 or rec.applied
            for rec in self.manifest.screens.values()
        )

    # -- manifest persistence -----------------------------------------

    @property
    def _host_manifest(self) -> Path:
        return self.host_dir / MANIFEST_NAME

    def _parse_manifest(self, raw: str | bytes) -> Manifest:
        try:
            data = json.loads(raw)
            if data.get("schema") != SCHEMA_VERSION or data.get("build") != self.build:
                raise ValueError("unsupported schema or firmware build mismatch")
            manifest = Manifest.from_json(data)
            for key, rec in manifest.screens.items():
                if rec.backup_file is not None and rec.backup_file != f"{key}.png":
                    raise ValueError("unexpected backup filename")
                if not re.fullmatch(r"[A-Za-z0-9_]+", key):
                    raise ValueError("invalid screen key")
                if rec.stock_sha256 is not None and (
                    not re.fullmatch(r"[a-f0-9]{64}", rec.stock_sha256) or not rec.backup_file
                ):
                    raise ValueError("invalid stock record")
            return manifest
        except (ValueError, TypeError, AttributeError, UnicodeDecodeError) as exc:
            raise BackupError(f"invalid backup manifest: {exc}") from exc

    def _save_host_manifest(self, manifest: Manifest) -> None:
        self.host_dir.mkdir(parents=True, exist_ok=True)
        temporary = self._host_manifest.with_suffix(".json.tmp")
        payload = json.dumps(manifest.to_json(), indent=2) + "\n"
        if self._host_manifest.is_file() and self._host_manifest.read_text() == payload:
            return
        temporary.write_text(payload)
        temporary.replace(self._host_manifest)

    def _load(self) -> Manifest:
        """Prefer the tablet's manifest: another computer may have updated it."""
        device_manifest = posixpath.join(self.device_dir, MANIFEST_NAME)
        if self.device.exists(device_manifest):
            manifest = self._parse_manifest(self.device.read_bytes(device_manifest))
            self.host_dir.mkdir(parents=True, exist_ok=True)
            for rec in manifest.screens.values():
                if not rec.backup_file:
                    continue
                local = self.host_dir / rec.backup_file
                if local.is_file() and hashlib.sha256(local.read_bytes()).hexdigest() == (
                    rec.stock_sha256
                ):
                    continue
                remote = posixpath.join(self.device_dir, rec.backup_file)
                if self.device.exists(remote):
                    data = self.device.read_bytes(remote)
                    if hashlib.sha256(data).hexdigest() != rec.stock_sha256:
                        raise BackupError(f"corrupt device backup: {rec.backup_file}")
                    local.write_bytes(data)
            self._save_host_manifest(manifest)
            return manifest
        if self._host_manifest.is_file():
            return self._parse_manifest(self._host_manifest.read_text())
        return Manifest(build=self.build, board=self.board)

    def save(self) -> None:
        """Persist the manifest to host, then mirror it to the device."""
        self.host_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.manifest.to_json(), indent=2) + "\n"
        self._save_host_manifest(self.manifest)
        # /home is already read-write, so no remount is needed here.
        self.device.check(f"mkdir -p {quote(self.device_dir)}")
        self.device.write_home_bytes(
            posixpath.join(self.device_dir, MANIFEST_NAME), payload.encode()
        )

    # -- capture ------------------------------------------------------

    def needs_capture(self, screen: Screen) -> bool:
        return self.manifest.record(screen.key).stock_sha256 is None

    #: How far after the build a file may be stamped and still be taken for
    #: stock. The firmware writes its files within one image build, so this
    #: only has to absorb ordering inside that, not weeks of use.
    BUILD_TOLERANCE_SECONDS = 600

    def _build_timestamp(self) -> int | None:
        """The build string as an epoch, or None if it is not a timestamp.

        Builds are named for when they were made, 20260827113527 being
        2026-08-27 11:35:27 UTC, and the firmware stamps the files it lays
        down with that time. A build named some other way simply disables
        the check rather than breaking it.
        """
        try:
            when = datetime.strptime(self.build, "%Y%m%d%H%M%S")
        except ValueError:
            return None
        return int(when.replace(tzinfo=UTC).timestamp())

    def _refuse_if_written_after_firmware(self, screen: Screen) -> None:
        """Refuse a screen that was written after the firmware was installed.

        Only reached when nothing on this computer or the tablet knew
        anything about this build, which is the one moment the tool cannot
        tell stock art from someone else's custom image. See
        StockLooksModified.
        """
        built = self._build_timestamp()
        if built is None:
            return
        try:
            written = int(self.device.mtime(screen.path))
        except Exception:
            # No timestamp, no evidence, no grounds to refuse. The other
            # two rules still stand, and a guard that cannot read a clock
            # must not be able to stop a capture that is otherwise sound.
            return
        if written <= built + self.BUILD_TOLERANCE_SECONDS:
            return
        raise StockLooksModified(
            f"{screen.key}: {screen.path} was last written "
            f"{_ago(written - built)} after firmware {self.build} was "
            f"installed, so it may be someone else's custom image rather "
            f"than stock art. Nothing on this computer or on the tablet "
            f"knows this build, which is what a factory reset leaves "
            f"behind: it erases the tablet's backups but not the screens. "
            f"Capturing now could record a custom image as the only "
            f"surviving copy of the original. If you know these are the "
            f"originals, for instance because you restored them, run "
            f"`rephemeral backup --assume-stock`."
        )

    def capture(self, screen: Screen, assume_stock: bool = False) -> ScreenRecord:
        """Back up one screen's current image, if not already captured.

        Write-once: an existing record is returned untouched.

        `assume_stock` overrides the modified-after-the-firmware refusal,
        for someone who knows the screens are the originals. It cannot
        override `StockAlreadyLost`, which rests on a recorded fact rather
        than on a heuristic.
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

        if self.arrived_empty and not assume_stock:
            self._refuse_if_written_after_firmware(screen)

        data = self.device.read_bytes(screen.path)
        if hashlib.sha256(data).hexdigest() != current:
            raise BackupError(
                f"{screen.key}: file changed while being read; try again"
            )

        filename = screen.key + ".png"
        self.host_dir.mkdir(parents=True, exist_ok=True)
        (self.host_dir / filename).write_bytes(data)

        self.device.check(f"mkdir -p {quote(self.device_dir)}")
        self.device.write_home_bytes(
            posixpath.join(self.device_dir, filename), data
        )

        rec.stock_sha256 = current
        rec.captured_at = _now()
        rec.backup_file = filename
        return rec

    def capture_all(self, screens: tuple[Screen, ...],
                    assume_stock: bool = False) -> dict[str, str]:
        """Capture every screen. Returns key -> status."""
        results: dict[str, str] = {}
        for s in screens:
            if not self.needs_capture(s):
                results[s.key] = "already backed up"
                continue
            try:
                self.capture(s, assume_stock=assume_stock)
                self.save()
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
