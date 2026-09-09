"""Local web UI for rePhemeral.

Binds to localhost by default. It is a control surface for a device on the
end of a USB cable, not a service: there is no authentication, because
anything that can reach it already has a shell on the machine that has the
tablet plugged into it. Do not bind it to a public interface.
"""
from __future__ import annotations

import base64
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from . import config, screens
from .apply import Applier
from .backup import BackupError, BackupStore
from .device import Device, DeviceError
from .images import ImageError, thumbnail

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="rePhemeral", docs_url=None, redoc_url=None)


def _device() -> Device:
    cfg = config.load()
    if not cfg.key.is_file():
        raise HTTPException(
            status_code=503,
            detail=f"No SSH key at {cfg.key}. Run `rephemeral setup` first.",
        )
    d = Device(host=cfg.host, key_path=str(cfg.key),
               username=cfg.username, port=cfg.port)
    try:
        d.connect()
    except DeviceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return d


def _screen(key: str):
    """Look up a screen, turning an unknown key into a 404 rather than a
    500. screens.get raises KeyError, which FastAPI would otherwise
    surface as an unhandled server error."""
    try:
        return screens.get(key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc


def _b64_png(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status() -> JSONResponse:
    try:
        d = _device()
    except HTTPException as exc:
        return JSONResponse({"connected": False, "detail": exc.detail}, status_code=200)
    try:
        info = d.info()
        store = BackupStore(d, build=info.build, board=info.board)
        drift = store.drift(screens.SCREENS)
        return JSONResponse({
            "connected": True,
            "board": info.board,
            "build": info.build,
            "kernel": info.kernel,
            "free_mb": round(info.free_mb, 1),
            "total_mb": round(info.total_bytes / 1024 / 1024),
            "backup_dir": str(store.host_dir),
            "screens": [
                {
                    "key": s.key,
                    "label": s.label,
                    "description": s.description,
                    "width": s.width,
                    "height": s.height,
                    "risky": s.risky,
                    "state": drift.get(s.key, "unknown"),
                    "has_backup": store.manifest.record(s.key).stock_sha256 is not None,
                }
                for s in screens.SCREENS
            ],
        })
    finally:
        d.close()


@app.get("/api/screen/{key}/current")
def current(key: str) -> JSONResponse:
    """Thumbnail of what is on the device right now."""
    screen = _screen(key)
    d = _device()
    try:
        if not d.exists(screen.path):
            raise HTTPException(status_code=404, detail="not present on device")
        return JSONResponse({"image": _b64_png(thumbnail(d.read_bytes(screen.path)))})
    finally:
        d.close()


@app.post("/api/screen/{key}/apply")
async def apply_screen(
    key: str,
    file: UploadFile = File(...),
    fit: str = Form("cover"),
    greyscale: bool = Form(False),
) -> JSONResponse:
    screen = _screen(key)
    raw = await file.read()
    d = _device()
    try:
        info = d.info()
        store = BackupStore(d, build=info.build, board=info.board)
        result = Applier(d, store).apply(
            screen, raw, source=file.filename or "upload",
            fit=fit, greyscale=greyscale,
        )
        return JSONResponse({
            "ok": True,
            "sha256": result.sha256,
            "size_kb": round(result.size_bytes / 1024, 1),
            "notes": result.notes,
            "backed_up_now": result.backed_up_now,
        })
    except (ImageError, BackupError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DeviceError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        d.close()


@app.post("/api/screen/{key}/restore")
def restore(key: str) -> JSONResponse:
    screen = _screen(key)
    d = _device()
    try:
        info = d.info()
        store = BackupStore(d, build=info.build, board=info.board)
        sha = Applier(d, store).restore(screen)
        return JSONResponse({"ok": True, "sha256": sha})
    except BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        d.close()


@app.post("/api/restore-all")
def restore_all() -> JSONResponse:
    d = _device()
    try:
        info = d.info()
        store = BackupStore(d, build=info.build, board=info.board)
        return JSONResponse({"results": Applier(d, store).restore_all(screens.SCREENS)})
    finally:
        d.close()


@app.post("/api/backup")
def backup() -> JSONResponse:
    d = _device()
    try:
        info = d.info()
        store = BackupStore(d, build=info.build, board=info.board)
        return JSONResponse({"results": store.capture_all(screens.SCREENS)})
    finally:
        d.close()


@app.post("/api/restart-ui")
def restart_ui() -> JSONResponse:
    d = _device()
    try:
        info = d.info()
        store = BackupStore(d, build=info.build, board=info.board)
        Applier(d, store).restart_ui()
        return JSONResponse({"ok": True})
    finally:
        d.close()
