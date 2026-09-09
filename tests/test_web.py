import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from rephemeral import web
from rephemeral.device import DeviceError


def client():
    return TestClient(web.app, base_url='http://127.0.0.1')


def test_local_page_and_cross_origin_protection():
    with client() as http:
        assert http.get('/').status_code == 200
        response = http.post('/api/backup', headers={'Origin': 'https://evil.example'})
        assert response.status_code == 403
        assert http.get('/', headers={'Host': 'evil.example'}).status_code == 400


def test_disconnect_is_json(monkeypatch):
    monkeypatch.setattr(web, '_device', MagicMock(side_effect=DeviceError('unplugged')))
    with client() as http:
        response = http.post('/api/screen/suspended/restore')
    assert response.status_code == 503
    assert response.json()['detail'] == 'unplugged'


def test_upload_limit(monkeypatch):
    monkeypatch.setattr(web, 'MAX_UPLOAD_BYTES', 3)
    connect = MagicMock()
    monkeypatch.setattr(web, '_device', connect)
    with client() as http:
        response = http.post('/api/screen/suspended/apply', files={'file': ('test.png', b'1234')})
    assert response.status_code == 413
    connect.assert_not_called()


def test_ping_stays_responsive_during_upload(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    device = MagicMock()
    monkeypatch.setattr(web, '_device', lambda: device)
    monkeypatch.setattr(web, 'BackupStore', MagicMock())
    probe = SimpleNamespace(socket=MagicMock(), AF_INET=2, SOCK_STREAM=1)
    monkeypatch.setattr(web, 'socket', probe)

    def apply(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        raise DeviceError('simulated disconnect')

    monkeypatch.setattr(web.Applier, 'apply', apply)
    with client() as http, ThreadPoolExecutor() as pool:
        future = pool.submit(http.post, '/api/screen/suspended/apply',
                             files={'file': ('image.png', b'image')})
        try:
            assert entered.wait(5)
            assert http.get('/api/ping').json() == {'reachable': True}
        finally:
            release.set()
        assert future.result().status_code == 500
    device.close.assert_called_once()


def test_device_operations_do_not_overlap(monkeypatch):
    entered, release, overlap = threading.Event(), threading.Event(), threading.Event()
    device = MagicMock()
    monkeypatch.setattr(web, '_device', lambda: device)

    def check(command):
        if entered.is_set():
            overlap.set()
        entered.set()
        assert release.wait(5)

    device.check.side_effect = check
    with client() as http, ThreadPoolExecutor() as pool:
        first = pool.submit(http.post, '/api/restart-ui')
        assert entered.wait(5)
        second = pool.submit(http.post, '/api/restart-ui')
        try:
            assert not overlap.wait(0.1)
        finally:
            release.set()
        assert first.result().status_code == 200
        assert second.result().status_code == 200
    assert device.check.call_count == 2


def test_ipv6_loopback_host_is_accepted():
    """[::1] must not be rejected.

    Starlette's TrustedHostMiddleware strips the port by splitting on the
    first colon, which turns "[::1]:8765" into "[" and matches no allowed
    host. Browsing to http://[::1]:8765 returned 400.
    """
    client = TestClient(web.app, base_url="http://127.0.0.1:8765")
    for host in ("[::1]:8765", "[::1]", "127.0.0.1:8765", "localhost:8765"):
        assert client.get("/", headers={"host": host}).status_code == 200, host
    for host in ("evil.example", "evil.example:8765", "[::1].evil.example"):
        assert client.get("/", headers={"host": host}).status_code == 400, host
