"""The inspector's route check and the skip cascade that follows it.

The socket module is replaced so no packet is sent and no tablet is
needed. What matters is which local address the OS says it would use, and
whether connect() raises, since those are the only two things the route
check can observe.
"""
from types import SimpleNamespace

import pytest

from rephemeral import config, inspector

AFTER_ROUTE = ['Port open', 'SSH', 'Device', 'Screens', 'Backups']


class FakeSocket:
    def __init__(self, kind, net):
        self.kind, self.net = kind, net
        net.opened.append(kind)

    def connect(self, address):
        if self.kind == 'udp' and self.net.route_error:
            raise OSError(101, 'Network is unreachable')
        if self.kind == 'tcp' and not self.net.tcp_ok:
            raise OSError(10060, 'timed out')

    def getsockname(self):
        return (self.net.local, 0)

    def settimeout(self, value):
        pass

    def close(self):
        pass


@pytest.fixture
def net(monkeypatch):
    state = SimpleNamespace(local='10.11.99.6', route_error=False, tcp_ok=False, opened=[])
    module = SimpleNamespace(
        AF_INET=2, SOCK_DGRAM=2, SOCK_STREAM=1,
        socket=lambda family, kind: FakeSocket('udp' if kind == 2 else 'tcp', state),
        gethostbyname=lambda host: host,
    )
    monkeypatch.setattr(inspector, 'socket', module)
    for name in ('_python', '_platform', '_deps'):
        monkeypatch.setattr(inspector, name, lambda n=name: inspector.Check(n, inspector.OK, ''))
    monkeypatch.setattr(inspector, '_config', lambda cfg: inspector.Check('Config', 'ok', ''))
    monkeypatch.setattr(inspector, '_key', lambda cfg: inspector.Check('SSH key', 'ok', ''))
    return state


def statuses(checks):
    return {c.name: c.status for c in checks}


def test_no_route_fails_and_skips_everything_after(net):
    net.route_error = True
    checks = inspector.run(config.Config())
    assert statuses(checks)['USB network'] == inspector.FAIL
    assert [c.name for c in checks[-5:]] == AFTER_ROUTE
    assert all(c.status == inspector.SKIP for c in checks[-5:])


def test_cable_out_with_a_default_route_fails_rather_than_warns(net):
    # What a laptop on Wi-Fi reports with the tablet unplugged: connect()
    # succeeds through the LAN gateway, from the Wi-Fi address.
    net.local = '192.168.0.4'
    checks = inspector.run(config.Config())
    route = next(c for c in checks if c.name == 'USB network')
    assert route.status == inspector.FAIL
    assert 'not up' in route.detail
    assert all(c.status == inspector.SKIP for c in checks[-5:])
    assert 'tcp' not in net.opened


def test_off_subnet_route_to_a_configured_host_only_warns(net):
    net.local = '10.0.0.3'
    checks = inspector.run(config.Config(host='192.168.5.20'))
    result = statuses(checks)
    assert result['USB network'] == inspector.WARN
    assert result['Port open'] == inspector.FAIL
    assert [result[n] for n in AFTER_ROUTE[1:]] == [inspector.SKIP] * 4


def test_tablet_subnet_is_ok_even_on_a_narrower_lease(net):
    # The tablet's DHCP hands out a /27; the check compares against a /24,
    # which contains it.
    route = inspector._route(config.Config())
    assert route.status == inspector.OK
    assert '10.11.99.6' in route.detail


def test_no_check_is_failed_after_a_skip(net):
    net.route_error = True
    seen_skip = False
    for check in inspector.run(config.Config()):
        seen_skip = seen_skip or check.status == inspector.SKIP
        assert not (seen_skip and check.status == inspector.FAIL)


def test_key_check_reports_what_is_true(tmp_path, monkeypatch):
    key = config.ensure_key(tmp_path / 'key')
    cfg = config.Config(key_path=str(key))
    assert inspector._key(cfg).status == inspector.OK

    monkeypatch.setattr(config, 'key_exposure', lambda path: ['BUILTIN\\Users'])
    exposed = inspector._key(cfg)
    assert exposed.status == inspector.WARN
    assert 'BUILTIN\\Users' in exposed.detail

    def unreadable(path):
        raise OSError('access denied')
    monkeypatch.setattr(config, 'key_exposure', unreadable)
    assert inspector._key(cfg).status == inspector.WARN

    assert inspector._key(config.Config(key_path=str(tmp_path / 'none'))).status == inspector.FAIL
