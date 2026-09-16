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

from rephemeral import cli, config, device, hostkey, paths

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


def test_offered_key_is_read_without_sending_anything(monkeypatch, keys):
    # The whole point: the key exchange precedes authentication, so this
    # can compare keys before a password is typed.
    server_key, _ = keys
    transport = MagicMock()
    transport.get_remote_server_key.return_value = server_key
    opened = {}

    def connect(address, timeout=None):
        opened.update(address=address, timeout=timeout)
        return MagicMock()

    monkeypatch.setattr('rephemeral.device.socket.create_connection', connect)
    monkeypatch.setattr('rephemeral.device.paramiko.Transport',
                        lambda sock: transport)

    got = device.offered_host_key(HOST, 22, timeout=4.0)

    assert device.fingerprint(got) == device.fingerprint(server_key)
    transport.start_client.assert_called_once()
    transport.close.assert_called_once()
    transport.auth_password.assert_not_called()
    transport.auth_publickey.assert_not_called()
    # The connect is bounded. Unbounded, an address that never answers
    # would stall setup before its prompt for as long as the OS allows.
    assert opened == {'address': (HOST, 22), 'timeout': 4.0}


def test_offered_key_reports_an_unreachable_host_as_a_device_error(monkeypatch):
    def refuse(address, timeout=None):
        raise OSError('connection refused')
    monkeypatch.setattr('rephemeral.device.socket.create_connection', refuse)
    with pytest.raises(device.DeviceError):
        device.offered_host_key(HOST, 22, timeout=1.0)


def test_hostkey_offered_delegates_to_the_same_reader(monkeypatch, keys):
    server_key, _ = keys
    seen = {}

    def reader(host, port, timeout):
        seen.update(host=host, port=port, timeout=timeout)
        return server_key

    monkeypatch.setattr(device, 'offered_host_key', reader)
    assert hostkey.offered(HOST, 2222, timeout=3.0) is server_key
    assert seen == {'host': HOST, 'port': 2222, 'timeout': 3.0}


def test_record_replaces_whatever_is_there(store, keys):
    first, second = keys
    assert hostkey.record(HOST, first, path=store) == device.fingerprint(first)
    assert hostkey.recorded_fingerprint(HOST, path=store) == device.fingerprint(first)
    # Replacing rather than appending: a host has one key, not a pile.
    assert hostkey.record(HOST, second, path=store) == device.fingerprint(second)
    assert hostkey.recorded_fingerprint(HOST, path=store) == device.fingerprint(second)
    assert len(store.read_text().strip().splitlines()) == 1
    assert config.key_exposure(store) == []


@pytest.fixture
def setup_run(tmp_path, monkeypatch):
    """`rephemeral setup` with its store redirected and the tablet replaced.

    Returns a callable taking the password behaviour, so each test drives
    the prompt the way it needs: a string, or an exception to raise.
    """
    store = tmp_path / 'known_hosts'
    monkeypatch.setattr(hostkey, 'STORE_PATH', store)
    # cmd_setup ends with cfg.save(), which writes to config.CONFIG_PATH.
    # Without this the test rewrites the real ~/.config/rephemeral, pointing
    # it at a pytest temporary directory that is deleted afterwards. It did
    # exactly that once; a test that can damage the machine it runs on is a
    # defect in the test.
    monkeypatch.setattr(config, 'CONFIG_PATH', tmp_path / 'rephemeral.toml')
    # And cmd_setup now restricts config.CONFIG_DIR when no override is set,
    # so without this the suite would chmod the real folder on any machine
    # whose own folder is group-writable. Same lesson as the line above.
    monkeypatch.setattr(config, 'CONFIG_DIR', tmp_path)
    monkeypatch.setattr(cli.config, 'load', lambda: config.Config(
        key_path=str(config.ensure_key(tmp_path / 'id_ed25519'))))
    monkeypatch.setattr(cli.config, 'ensure_key', lambda path=None: tmp_path / 'id_ed25519')
    monkeypatch.setattr(cli.config, 'public_key_line', lambda path=None: 'ssh-ed25519 AAAA x')
    monkeypatch.setattr(cli.migrate, 'run', lambda: cli.migrate.Report())

    def run(password, install=None):
        def prompt(_):
            if isinstance(password, BaseException):
                raise password
            return password
        monkeypatch.setattr(cli, '_read_password', prompt)
        monkeypatch.setattr(cli.config, 'install_key', install or (lambda *a, **k: None))
        monkeypatch.setattr(cli, '_connect', MagicMock())
        return cli.main(['setup', '--trust-new-key'])

    return store, run


def test_setup_keeps_the_pin_when_the_prompt_reads_nothing(setup_run, keys, capsys):
    # The failure that prompted this: --trust-new-key forgot the key first,
    # so a prompt that never read left the tablet un-pinned and silent.
    store, run = setup_run
    hostkey.record(HOST, keys[0], path=store)

    assert run(EOFError()) == 1

    assert hostkey.recorded_fingerprint(HOST, path=store) == device.fingerprint(keys[0])
    err = capsys.readouterr().err
    assert 'no input' in err.lower()
    assert 'Aborted.' not in err          # not the same event as Ctrl+C


def test_setup_says_aborted_only_when_interrupted(setup_run, capsys):
    _, run = setup_run
    assert run(KeyboardInterrupt()) == 1
    assert 'Aborted.' in capsys.readouterr().err


def test_setup_puts_the_old_key_back_when_installing_fails(setup_run, keys, capsys):
    store, run = setup_run
    hostkey.record(HOST, keys[0], path=store)

    def explode(*args, **kwargs):
        raise RuntimeError('wrong password')

    assert run('hunter2', install=explode) == 1
    assert hostkey.recorded_fingerprint(HOST, path=store) == device.fingerprint(keys[0])
    assert 'put back' in capsys.readouterr().err.lower()


def test_setup_forgets_once_the_password_is_in_hand(setup_run, keys, capsys):
    store, run = setup_run
    hostkey.record(HOST, keys[0], path=store)

    assert run('hunter2') == 0

    # Forgotten for real: the connection is mocked here, so nothing records
    # a replacement, which is exactly what makes the forget visible.
    assert hostkey.recorded(HOST, path=store) is None
    assert 'Forgot the host key' in capsys.readouterr().out


def test_setup_restricts_its_own_config_folder(setup_run, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv('REPHEMERAL_CONFIG_DIR', raising=False)
    _, run = setup_run
    if os.name == 'nt':
        from rephemeral import winacl
        winacl.set_dacl(tmp_path, f'D:P(A;;FA;;;{winacl.current_user_sid()})(A;;FA;;;BU)')
    else:
        tmp_path.chmod(0o775)

    assert run('hunter2') == 0

    assert config.dir_exposure(tmp_path) == []
    assert 'Restricted' in capsys.readouterr().out


def test_setup_leaves_an_overridden_config_folder_alone(setup_run, tmp_path,
                                                        monkeypatch, capsys):
    # A folder someone pointed REPHEMERAL_CONFIG_DIR at may be shared on
    # purpose, so setup reports nothing and changes nothing there.
    monkeypatch.setenv('REPHEMERAL_CONFIG_DIR', str(tmp_path))
    _, run = setup_run
    if os.name != 'nt':
        tmp_path.chmod(0o775)

        assert run('hunter2') == 0

        assert tmp_path.stat().st_mode & 0o777 == 0o775
        assert 'Restricted' not in capsys.readouterr().out


@pytest.fixture
def trust_key_run(tmp_path, monkeypatch):
    """`rephemeral trust-key` with its store redirected and the tablet faked."""
    store = tmp_path / 'known_hosts'
    monkeypatch.setattr(hostkey, 'STORE_PATH', store)
    monkeypatch.setattr(config, 'CONFIG_PATH', tmp_path / 'rephemeral.toml')
    monkeypatch.setattr(config, 'CONFIG_DIR', tmp_path)
    monkeypatch.setattr(cli.config, 'load', lambda: config.Config())
    monkeypatch.setattr(cli.migrate, 'run', lambda: cli.migrate.Report())

    def run(offers):
        def reader(host, port, timeout=10.0):
            if isinstance(offers, BaseException):
                raise offers
            return offers
        monkeypatch.setattr(hostkey, 'offered', reader)
        return cli.main(['trust-key'])

    return store, run


def test_trust_key_records_a_first_key_without_a_password(trust_key_run, keys, capsys):
    store, run = trust_key_run
    assert run(keys[0]) == 0
    assert hostkey.recorded_fingerprint(HOST, path=store) == device.fingerprint(keys[0])
    assert 'Recorded the host key' in capsys.readouterr().out


def test_trust_key_replaces_a_changed_key_and_shows_both(trust_key_run, keys, capsys):
    store, run = trust_key_run
    hostkey.record(HOST, keys[0], path=store)

    assert run(keys[1]) == 0

    assert hostkey.recorded_fingerprint(HOST, path=store) == device.fingerprint(keys[1])
    out = capsys.readouterr().out
    assert device.fingerprint(keys[0]) in out and device.fingerprint(keys[1]) in out


def test_trust_key_does_nothing_when_the_key_is_unchanged(trust_key_run, keys, capsys):
    store, run = trust_key_run
    hostkey.record(HOST, keys[0], path=store)
    before = store.read_bytes()

    assert run(keys[0]) == 0

    assert store.read_bytes() == before
    assert 'Nothing to do' in capsys.readouterr().out


def test_trust_key_changes_nothing_when_the_tablet_cannot_be_reached(trust_key_run,
                                                                    keys, capsys):
    store, run = trust_key_run
    hostkey.record(HOST, keys[0], path=store)
    before = store.read_bytes()

    assert run(device.DeviceError('could not read the host key')) == 1

    assert store.read_bytes() == before
    assert 'Nothing was changed' in capsys.readouterr().err


def test_setup_refuses_a_changed_key_before_asking_for_a_password(setup_run, keys,
                                                                  monkeypatch, capsys):
    store, run = setup_run
    hostkey.record(HOST, keys[0], path=store)
    monkeypatch.setattr(hostkey, 'offered',
                        lambda host, port, timeout=10.0: keys[1])
    asked = []
    monkeypatch.setattr(cli, '_read_password', lambda prompt: asked.append(prompt))

    # run() replaces _read_password itself, so call main directly here.
    assert cli.main(['setup']) == 1

    assert asked == []                      # never reached the prompt
    err = capsys.readouterr().err
    assert device.fingerprint(keys[0]) in err and device.fingerprint(keys[1]) in err
    assert hostkey.recorded_fingerprint(HOST, path=store) == device.fingerprint(keys[0])


def test_a_changed_key_is_a_device_error(client, store, keys):
    # Callers that only know about DeviceError still fail closed: the web
    # app turns it into a 503, and the inspector into a failed SSH check.
    offered, expected = keys
    client.connect.side_effect = paramiko.BadHostKeyException(HOST, offered, expected)
    with pytest.raises(device.DeviceError):
        device.Device(known_hosts=str(store)).connect()
    client.open_sftp.assert_not_called()
