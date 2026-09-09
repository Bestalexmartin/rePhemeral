"""Orchestration: capture, convert, write, restore.

The ordering rule this module exists to enforce is that a screen's stock
image is captured before that screen is ever written. Not at install time,
not on a best-effort basis: immediately before the first write to that
specific file, every time, with the write refused if the capture failed.
Any other ordering has a window in which the stock art is gone and no
backup exists.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .backup import BackupStore
from .device import Device
from .images import Prepared, prepare
from .screens import Screen


@dataclass
class ApplyResult:
    screen: Screen
    sha256: str
    size_bytes: int
    notes: list[str]
    backed_up_now: bool
    """True if this call performed the stock capture, rather than finding
    an existing backup."""


class Applier:
    def __init__(self, device: Device, store: BackupStore) -> None:
        self.device = device
        self.store = store

    def apply(
        self,
        screen: Screen,
        image_bytes: bytes,
        *,
        source: str = "upload",
        fit: str = "cover",
        background: tuple[int, int, int] = (255, 255, 255),
        grayscale: bool = False,
    ) -> ApplyResult:
        """Convert and write one image, capturing stock art first."""
        # 1. Convert before touching the device. A conversion failure
        #    should cost nothing, and must not leave the rootfs remounted.
        prepared: Prepared = prepare(
            image_bytes, screen, fit=fit, background=background, grayscale=grayscale
        )

        # 2. Capture stock art. Raises rather than proceeding: a write
        #    without a backup is the one outcome that cannot be undone.
        backed_up_now = self.store.needs_capture(screen)
        if backed_up_now:
            self.store.capture(screen)
            self.store.save()

        # Verify an existing record still has recoverable stock before overwriting.
        self.store.stock_bytes(screen)
        # Persist intent before the write, so a disconnect cannot lose its hash.
        self.store.note_applied(screen, hashlib.sha256(prepared.data).hexdigest(), source)
        self.store.save()

        # 3. Write, inside the remount guard.
        with self.device.writable_rootfs():
            sha = self.device.write_bytes(screen.path, prepared.data)

        return ApplyResult(
            screen=screen,
            sha256=sha,
            size_bytes=prepared.size_bytes,
            notes=prepared.notes,
            backed_up_now=backed_up_now,
        )

    def restore(self, screen: Screen) -> str:
        """Put the stock image back. Returns the resulting sha256."""
        data = self.store.stock_bytes(screen)
        with self.device.writable_rootfs():
            sha = self.device.write_bytes(screen.path, data)
        return sha

    def restore_all(self, screens: tuple[Screen, ...]) -> dict[str, str]:
        results: dict[str, str] = {}
        # One remount for the whole batch rather than one per file: less
        # time with a writable rootfs is strictly better.
        with self.device.writable_rootfs():
            for s in screens:
                rec = self.store.manifest.record(s.key)
                if rec.stock_sha256 is None:
                    results[s.key] = "no backup; skipped"
                    continue
                try:
                    current = self.device.sha256(s.path)
                    if current == rec.stock_sha256:
                        results[s.key] = "already stock"
                        continue
                    data = self.store.stock_bytes(s)
                    self.device.write_bytes(s.path, data)
                    results[s.key] = "restored"
                except Exception as exc:
                    results[s.key] = f"FAILED: {exc}"
        return results

    def restart_ui(self) -> None:
        """Restart xochitl, the tablet's UI process.

        Not needed after a screen change. Confirmed on firmware build
        20260827113527: xochitl reads these PNGs when it needs to draw
        them rather than caching them at startup, so a written screen is
        live immediately. Kept because restarting the UI is occasionally
        useful for its own sake, but it blanks the display and returns to
        the document list, so it is never done implicitly.
        """
        self.device.check("systemctl restart xochitl")
