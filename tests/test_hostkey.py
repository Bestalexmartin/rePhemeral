"""The tablet's recorded host key: what is stored, and what a change does.

The store is exercised for real. The connection is not: these tests replace
paramiko's client, because what matters on this side is which file the
client is pointed at, and what the tool does with the exception paramiko
raises when a recorded key does not match. That paramiko checks the host
key before authenticating is its own behaviour, not ours: it compares keys
in `SSHClient.connect` before the auth flow, so the refusal below happens
with nothing sent.
"""
import os
from unittest.mock import MagicMock

import paramiko
import pytest

from rephemeral import config, device, hostkey, paths

HOST = '10.11.99.1'


@pytest.fixture(scope='module')
def keys():
    """Two host keys, generated once because RSA generation is slow.

    RSA rather than ed25519 because paramiko's Ed25519Key cannot generate
    one; nothing here depends on the algorithm.
    """
    return paramiko.RSAKey.generate(2048), paramiko.RSAKey.generate(2048)


@pytest.fixture
def store(tmp_path):
    return tmp_path / 'config' / 'known_hosts'


@pytest.fixture
def client(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr('rephemeral.device.paramiko.SSHClient', lambda: fake)
    return fake


def test_entry_name_matches_paramikos_own_form():
    # paramiko files a key under the bare host on port 22 and under
    # [host]:port otherwise (client.py). A mismatch here would make every
    # connection look like first contact.
    assert hostkey.entry_name(HOST) == HOST
    assert hostkey.entry_name(HOST, 22) == HOST
    assert hostkey.entry_name(HOST, 2222) == f'[{HOST}]:2222'


def test_store_is_created_private_and_restored_if_loosened(store):
    path = hostkey.ensure_store(store)
    assert path.is_file() and path.read_bytes() == b''
    assert config.key_exposure(path) == []
    if os.name != 'nt':
        assert path.stat().st_mode & 0o777 == 0o600
        # paramiko's save writes the file itself at whatever the umask
        # allows, so ensure_store has to put the restriction back.
        path.chmod(0o644)
        hostkey.ensure_store(store)
        assert config.key_exposure(path) == []


def test_recording_then_forgetting_a_key(store, keys):
    first, second = keys
    hostkey.ensure_store(store)
    assert hostkey.recorded(HOST, path=store) is None
    assert hostkey.recorded_fingerprint(HOST, path=store) is None

    recorded = paramiko.HostKeys(str(store))
    recorded.add(HOST, first.get_name(), first)
    recorded.save(str(store))

    assert hostkey.recorded_fingerprint(HOST, path=store) == device.fingerprint(first)
    # A different port is a different entry, as it is for ssh(1).
    assert hostkey.recorded_fingerprint(HOST, 2222, path=store) is None
    assert device.fingerprint(first) != device.fingerprint(second)

    dropped = hostkey.forget(HOST, path=store)
    assert dropped == device.fingerprint(first)
    assert hostkey.recorded(HOST, path=store) is None
    # Forgetting what is not there is not an error, so `setup
    # --trust-new-key` works on a fresh install.
    assert hostkey.forget(HOST, path=store) is None


def _connectable(store):
    """A Device that will connect against the fake client.

    The identity check is stubbed because a MagicMock cannot answer
    `uname -n`; what these tests are about is which file the client is
    pointed at, which happens before any of that.
    """
    d = device.Device(known_hosts=str(store))
    d._assert_is_remarkable = lambda: None
    return d


def test_connect_points_the_client_at_the_store(client, store):
    hostkey.ensure_store(store)
    _connectable(store).connect()
    client.load_host_keys.assert_called_once_with(str(store))


def test_connect_still_records_when_no_store_exists_yet(client, store):
    # load_host_keys raises when the file is missing, and AutoAddPolicy only
    # saves the key it accepts when the filename is set, so first contact on
    # a fresh install would otherwise record nothing.
    client.load_host_keys.side_effect = OSError('no such file')
    _connectable(store).connect()
    assert client._host_keys_filename == str(store)


def test_default_store_travels_with_the_configuration():
    assert device.Device().known_hosts == str(paths.KNOWN_HOSTS)
    assert hostkey.STORE_PATH == paths.KNOWN_HOSTS


def test_a_changed_key_refuses_and_names_both_fingerprints(client, store, keys):
    offered, expected = keys
    client.connect.side_effect = paramiko.BadHostKeyException(HOST, offered, expected)
    d = device.Device(key_path='/keys/id_ed25519', known_hosts=str(store))

    with pytest.raises(device.HostKeyChangedError) as raised:
        d.connect()

    message = str(raised.value)
    assert device.fingerprint(expected) in message
    assert device.fingerprint(offered) in message
    assert 'setup --trust-new-key' in message
    assert str(store) in message
    # The session is dropped, and the password is not left on the object.
    client.close.assert_called_once()
    assert d._client is None
    assert d._password is None


def test_a_changed_key_is_a_device_error(client, store, keys):
    # Callers that only know about DeviceError still fail closed: the web
    # app turns it into a 503, and the inspector into a failed SSH check.
    offered, expected = keys
    client.connect.side_effect = paramiko.BadHostKeyException(HOST, offered, expected)
    with pytest.raises(device.DeviceError):
        device.Device(known_hosts=str(store)).connect()
    client.open_sftp.assert_not_called()
