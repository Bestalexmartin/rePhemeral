"""Local web UI for rePhemeral.

Binds to localhost by default. It is a control surface for a device on the
end of a USB cable, not a service. Browser origins and Host headers are
restricted to protect it from other websites; local processes still have
access without authentication. Do not bind it to a public interface.
"""
from __future__ import annotations

import base64
import socket
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from paramiko import SSHException

from . import config, screens
from .apply import Applier
from .backup import BackupError, BackupStore
from .device import Device, DeviceError
from .images import ImageError, thumbnail

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="rePhemeral", docs_url=None, redoc_url=None)

#: The UI binds to loopback, so any other Host header is a misconfiguration
#: or an attempt at DNS rebinding. An IPv6 literal appears bracketed here
#: because RFC 7230 requires brackets in a Host header; a bare "::1" would
#: be malformed, and listing it would be an entry nothing can match.
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})

_device_lock = threading.Lock()
MAX_UPLOAD_BYTES = 32 * 1024 * 1024


def _serial_device():
    # Different requests must not remount read-only during another request's write.
    with _device_lock:
        yield


def _host_only(value: str) -> str:
    """The host part of a Host header, with any port removed.

    Splitting on the first colon is right for `localhost:8765` and wrong
    for an IPv6 literal: `[::1]:8765` would become `[`. Bracketed literals
    have to be read to their closing bracket instead. Starlette's
    TrustedHostMiddleware splits, which is why this check is done here
    rather than delegated to it.
    """
    value = value.strip()
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return value
        rest = value[end + 1:]
        # Only a port may follow the closing bracket. Without this,
        # "[::1].evil.example" would read as "[::1]" and be allowed.
        if rest and not rest.startswith(":"):
            return value
        return value[: end + 1]
    return value.split(":", 1)[0]


@app.middleware("http")
async def local_requests(request: Request, call_next):
    if _host_only(request.headers.get("host", "")) not in ALLOWED_HOSTS:
        return JSONResponse({"detail": "Invalid host header."}, status_code=400)
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
        return JSONResponse({"detail": "Cross-origin requests are not allowed."}, status_code=403)
    if request.headers.get("sec-fetch-site") == "cross-site":
        return JSONResponse({"detail": "Cross-site requests are not allowed."}, status_code=403)
    return await call_next(request)


async def operation_error(request: Request, exc: Exception):
    code = 400 if isinstance(exc, (ImageError, BackupError)) else 503
    return JSONResponse({"detail": str(exc) or "Tablet connection failed."}, status_code=code)


for error_type in (DeviceError, SSHException, OSError, ImageError, BackupError):
    app.add_exception_handler(error_type, operation_error)


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


@app.get("/api/ping")
def ping() -> JSONResponse:
    """Cheap reachability probe for the browser's connection watcher.

    A bare TCP connect, deliberately not an SSH session. The watcher runs
    every couple of seconds while nothing is attached, and opening an SSH
    session at that rate would be wasteful on both ends.

    It answers "is something listening", not "can we use it". Those differ:
    before Developer Mode is enabled a Paper Pro accepts the connection and
    then resets it. So a transition to reachable only prompts a full
    /api/status read, which does the real work and reports a real error if
    the tablet is there but unusable.
    """
    cfg = config.load()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1.0)
    try:
        sock.connect((cfg.host, cfg.port))
        return JSONResponse({"reachable": True})
    except OSError:
        return JSONResponse({"reachable": False})
    finally:
        sock.close()


@app.get("/api/status", dependencies=[Depends(_serial_device)])
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


@app.get("/api/screen/{key}/current", dependencies=[Depends(_serial_device)])
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


@app.post("/api/screen/{key}/apply", dependencies=[Depends(_serial_device)])
def apply_screen(
    key: str,
    file: UploadFile = File(...),
    fit: str = Form("cover"),
    grayscale: bool = Form(False),
) -> JSONResponse:
    screen = _screen(key)
    raw = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Image uploads are limited to 32 MB.")
    d = _device()
    try:
        info = d.info()
        store = BackupStore(d, build=info.build, board=info.board)
        result = Applier(d, store).apply(
            screen, raw, source=file.filename or "upload",
            fit=fit, grayscale=grayscale,
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


@app.post("/api/screen/{key}/restore", dependencies=[Depends(_serial_device)])
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


@app.post("/api/restore-all", dependencies=[Depends(_serial_device)])
def restore_all() -> JSONResponse:
    d = _device()
    try:
        info = d.info()
        store = BackupStore(d, build=info.build, board=info.board)
        return JSONResponse({"results": Applier(d, store).restore_all(screens.SCREENS)})
    finally:
        d.close()


@app.post("/api/backup", dependencies=[Depends(_serial_device)])
def backup() -> JSONResponse:
    d = _device()
    try:
        info = d.info()
        store = BackupStore(d, build=info.build, board=info.board)
        return JSONResponse({"results": store.capture_all(screens.SCREENS)})
    finally:
        d.close()


@app.post("/api/restart-ui", dependencies=[Depends(_serial_device)])
def restart_ui() -> JSONResponse:
    d = _device()
    try:
        d.check("systemctl restart xochitl")
        return JSONResponse({"ok": True})
    finally:
        d.close()
