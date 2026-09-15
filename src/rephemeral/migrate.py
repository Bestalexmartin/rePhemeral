"""Carrying an existing install over to the current locations.

On Windows the tool used to keep its files where it does on Linux, under
~\\.config and ~\\.local\\share. Those installs hold the SSH key and, more
importantly, the host copy of the tablet's stock artwork. An orphaned
backup directory is a lost stock copy, so nothing here moves or deletes
anything. Files are copied, each copy is checked against its source, and
the originals stay where they were until the user removes them.

Wherever the current and legacy locations are the same, as they always
are on macOS and Linux, this does nothing.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from . import config, paths
from .backup import MANIFEST_NAME


@dataclass
class Report:
    copied: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def migrate_config(old: Path, new: Path, report: Report) -> None:
    """Copy the key, its public half and the config from old to new.

    Only into a location that holds neither a key nor a config, so a
    location already in use is never overwritten. A config whose key_path
    named the old key is rewritten to name the copy.
    """
    old_key, new_key = old / config.KEY_NAME, new / config.KEY_NAME
    old_toml, new_toml = old / config.CONFIG_NAME, new / config.CONFIG_NAME
    if _same(old, new) or not (old_key.is_file() or old_toml.is_file()):
        return
    if new_key.exists() or new_toml.exists():
        return
    config.make_private_dir(new)
    created: list[Path] = []
    try:
        if old_key.is_file():
            data = old_key.read_bytes()
            config.write_private(new_key, data)
            created.append(new_key)
            if _sha256(new_key.read_bytes()) != _sha256(data):
                raise OSError(f"the copy of {old_key} does not match the original")
            if config.pub_path(old_key).is_file():
                config.pub_path(new_key).write_bytes(config.pub_path(old_key).read_bytes())
                created.append(config.pub_path(new_key))
        if old_toml.is_file():
            cfg = config.load(old_toml)
            if old_key.is_file() and _same(Path(cfg.key_path), old_key):
                cfg.key_path = str(new_key)
            created.append(new_toml)
            cfg.save(new_toml)
    except BaseException:
        # Leave nothing half-copied behind, or the next run would find the
        # new location "in use" and never finish.
        for path in created:
            path.unlink(missing_ok=True)
        raise
    report.copied.append(f"the configuration and SSH key from {old} to {new}")


def _stock_hashes(build: Path, report: Report) -> dict[str, str]:
    manifest = build / MANIFEST_NAME
    if not manifest.is_file():
        return {}
    try:
        screens = json.loads(manifest.read_text()).get("screens") or {}
        return {rec["backup_file"]: rec["stock_sha256"] for rec in screens.values()
                if rec.get("backup_file") and rec.get("stock_sha256")}
    except (ValueError, AttributeError, TypeError) as exc:
        report.problems.append(
            f"{manifest} could not be read ({exc}), so its backups were copied "
            f"without being checked against their stock hashes."
        )
        return {}


def migrate_backups(old: Path, new: Path, report: Report) -> None:
    """Copy every backup build from old to new, file by file.

    A file already present in new is never replaced. Each copy is written
    to a temporary name, checked against the original, and only then
    renamed into place.
    """
    if _same(old, new) or not old.is_dir():
        return
    copied = 0
    for build in sorted(p for p in old.iterdir() if p.is_dir()):
        stock = _stock_hashes(build, report)
        for source in sorted(p for p in build.iterdir() if p.is_file()):
            if source.name.endswith(".tmp"):
                continue
            data = source.read_bytes()
            digest = _sha256(data)
            if source.name in stock and stock[source.name] != digest:
                report.problems.append(
                    f"{source} does not match the stock hash its manifest records. "
                    f"It was copied as it is, and restoring from it will be refused."
                )
            target = new / build.name / source.name
            if target.exists():
                # The manifest is rewritten as the tablet's changes are synced
                # down, so a newer one in the new location is expected. A
                # backup image is written once, so a different one is not.
                if source.name != MANIFEST_NAME and _sha256(target.read_bytes()) != digest:
                    report.problems.append(
                        f"{target} already exists and differs from {source}. "
                        f"Both have been left as they are."
                    )
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f"{target.name}.{uuid4().hex}.tmp")
            temporary.write_bytes(data)
            if _sha256(temporary.read_bytes()) != digest:
                temporary.unlink()
                report.problems.append(
                    f"The copy of {source} did not match the original and was "
                    f"discarded. The original is untouched."
                )
                continue
            temporary.replace(target)
            copied += 1
    if copied:
        report.copied.append(f"{copied} backup file(s) from {old} to {new}")


def run(environ: Mapping[str, str] = os.environ) -> Report:
    """Migrate from the legacy locations, unless an override chose a location."""
    report = Report()
    old_config, old_data = paths.legacy_dirs()
    if not environ.get("REPHEMERAL_CONFIG_DIR"):
        migrate_config(old_config, paths.CONFIG_DIR, report)
    if not environ.get("REPHEMERAL_DATA_DIR"):
        migrate_backups(old_data / "backups", paths.BACKUP_DIR, report)
    return report


def leftovers(environ: Mapping[str, str] = os.environ) -> dict[str, Path]:
    """Legacy directories still on disk beside the current ones, by kind."""
    old_config, old_data = paths.legacy_dirs()
    found: dict[str, Path] = {}
    if (not environ.get("REPHEMERAL_CONFIG_DIR") and old_config.is_dir()
            and not _same(old_config, paths.CONFIG_DIR)):
        found["config"] = old_config
    old_backups = old_data / "backups"
    if (not environ.get("REPHEMERAL_DATA_DIR") and old_backups.is_dir()
            and not _same(old_backups, paths.BACKUP_DIR)):
        found["backups"] = old_backups
    return found
