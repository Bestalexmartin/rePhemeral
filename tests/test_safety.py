import hashlib
import io
import json
import os
from unittest.mock import MagicMock

import pytest
from PIL import Image

from rephemeral import config, screens
from rephemeral.apply import Applier
from rephemeral.backup import BackupError, BackupStore, Manifest
from rephemeral.device import MIN_FREE_BYTES, Device, DeviceError, InsufficientSpaceError


def test_config_roundtrip(tmp_path):
    cfg = config.Config(host='example"host', key_path=r'C:\Users\A\key')
    path = cfg.save(tmp_path / 'config.toml')
    assert config.load(path) == cfg


def test_key_created_private_and_reused(tmp_path):
    path = tmp_path / 'key'
    config.ensure_key(path)
    original = path.read_bytes()
    # A mode on POSIX, an ACL on Windows: either way nobody else can read it.
    assert config.key_exposure(path) == []
    if os.name != 'nt':
        assert path.stat().st_mode & 0o777 == 0o600
    config.ensure_key(path)
    assert path.read_bytes() == original
    assert config.public_key_line(path).startswith('ssh-ed25519 ')


def _loosen(path):
    """Make a file readable by other accounts, the way each platform allows."""
    if os.name == 'nt':
        from rephemeral import winacl
        winacl.set_dacl(path, f'D:P(A;;FA;;;{winacl.current_user_sid()})(A;;FR;;;BU)')
    else:
        path.chmod(0o644)


def test_loosened_key_is_reported_then_restricted_again(tmp_path):
    path = config.ensure_key(tmp_path / 'key')
    _loosen(path)
    assert config.key_exposure(path) != []
    config.ensure_key(path)
    assert config.key_exposure(path) == []


def test_key_bytes_never_reach_an_unrestricted_file(tmp_path, monkeypatch):
    path = tmp_path / 'secret'
    sizes = []
    real = config.restrict_private
    monkeypatch.setattr(config, 'restrict_private',
                        lambda p: sizes.append(p.stat().st_size) or real(p))
    config.write_private(path, b'private')
    assert sizes == [0]
    assert path.read_bytes() == b'private'


def test_failed_restriction_leaves_no_file(tmp_path, monkeypatch):
    path = tmp_path / 'secret'
    monkeypatch.setattr(config, 'restrict_private', MagicMock(side_effect=OSError('denied')))
    with pytest.raises(OSError):
        config.write_private(path, b'private')
    assert not path.exists()


def test_private_dir_is_restricted_only_when_created(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(config, 'restrict_private', calls.append)
    config.make_private_dir(tmp_path)
    assert calls == []
    config.make_private_dir(tmp_path / 'new' / 'rephemeral')
    assert calls == [tmp_path / 'new' / 'rephemeral']


@pytest.mark.parametrize('failure', ['connect', 'sftp', 'identity'])
def test_failed_connect_closes_client(monkeypatch, failure):
    client = MagicMock()
    monkeypatch.setattr('rephemeral.device.paramiko.SSHClient', lambda: client)
    device = Device(password='temporary')
    if failure == 'connect':
        client.connect.side_effect = OSError('disconnected')
    elif failure == 'sftp':
        client.open_sftp.side_effect = OSError('no sftp')
    else:
        monkeypatch.setattr(device, '_assert_is_remarkable',
                            MagicMock(side_effect=DeviceError('wrong host')))
    with pytest.raises(DeviceError):
        device.connect()
    client.close.assert_called_once()
    assert device._client is None
    assert device._password is None


def test_temporary_upload_must_preserve_space_floor(monkeypatch):
    device = Device()
    device._sftp = MagicMock()
    device._rw_depth = 1
    monkeypatch.setattr(device, 'is_symlink', lambda path: False)
    monkeypatch.setattr(device, '_rootfs_space', lambda: (MIN_FREE_BYTES + 2, 100000000))
    with pytest.raises(InsufficientSpaceError):
        device.write_bytes('/usr/share/remarkable/suspended.png', b'123')
    device._sftp.putfo.assert_not_called()


@pytest.mark.parametrize('options', ['', 'rw,relatime', 'relatime'])
def test_remount_verification_fails_closed(monkeypatch, options):
    device = Device()
    run = MagicMock(return_value=(0, '', ''))
    monkeypatch.setattr(device, 'run', run)
    monkeypatch.setattr(device, 'check', lambda command: options)
    with pytest.raises(DeviceError):
        with device.writable_rootfs():
            pass
    assert device._rw_depth == 1
    device.close()
    assert device._rw_depth == 0
    assert run.call_args.args == ('sync; mount -o remount,ro /',)


def test_nested_remount_and_exception(monkeypatch):
    device = Device()
    run = MagicMock(return_value=(0, '', ''))
    monkeypatch.setattr(device, 'run', run)
    monkeypatch.setattr(device, 'check', lambda command: 'ro,relatime')
    with pytest.raises(ValueError):
        with device.writable_rootfs(), device.writable_rootfs():
            raise ValueError('transfer failed')
    assert run.call_count == 2
    assert device._rw_depth == 0


def test_home_path_cannot_escape():
    device = Device()
    device._sftp = MagicMock()
    with pytest.raises(DeviceError):
        device.write_home_bytes('/home/../usr/screen.png', b'image')
    device._sftp.putfo.assert_not_called()


def test_device_manifest_wins_over_stale_host(tmp_path):
    stale = Manifest(build='123')
    current = Manifest(build='123')
    current.record('suspended').applied.append({'sha256': 'new'})
    directory = tmp_path / '123'
    directory.mkdir()
    (directory / 'manifest.json').write_text(json.dumps(stale.to_json()))
    device = MagicMock()
    device.exists.return_value = True
    device.read_bytes.return_value = json.dumps(current.to_json()).encode()
    store = BackupStore(device, '123', host_root=tmp_path)
    assert store.manifest.is_ours('new')
    assert json.loads((directory / 'manifest.json').read_text()) == current.to_json()


@pytest.mark.parametrize('build', ['../escape', '/absolute', "bad'build", ''])
def test_invalid_build_rejected(tmp_path, build):
    with pytest.raises(BackupError):
        BackupStore(MagicMock(), build, host_root=tmp_path)


def test_manifest_path_traversal_rejected(tmp_path):
    device = MagicMock()
    device.exists.return_value = True
    manifest = Manifest(build='123')
    manifest.record('suspended').backup_file = '../../outside'
    device.read_bytes.return_value = json.dumps(manifest.to_json()).encode()
    with pytest.raises(BackupError):
        BackupStore(device, '123', host_root=tmp_path)


def test_apply_records_hash_before_write_and_checks_backup():
    events = []
    device, store = MagicMock(), MagicMock()
    store.needs_capture.return_value = False
    store.stock_bytes.side_effect = lambda screen: events.append('stock')
    store.note_applied.side_effect = lambda *args: events.append('record')
    store.save.side_effect = lambda: events.append('save')
    device.write_bytes.side_effect = lambda path, data: (
        events.append('write') or hashlib.sha256(data).hexdigest()
    )
    raw = io.BytesIO()
    Image.new('RGB', (2, 2), 'white').save(raw, format='PNG')
    Applier(device, store).apply(screens.get('carousel_1'), raw.getvalue())
    assert events == ['stock', 'record', 'save', 'write']


def test_missing_stock_blocks_apply():
    device, store = MagicMock(), MagicMock()
    store.needs_capture.return_value = False
    store.stock_bytes.side_effect = BackupError('missing')
    raw = io.BytesIO()
    Image.new('RGB', (2, 2)).save(raw, format='PNG')
    with pytest.raises(BackupError):
        Applier(device, store).apply(screens.get('carousel_1'), raw.getvalue())
    device.write_bytes.assert_not_called()


@pytest.mark.skipif(os.name == 'nt', reason='runs the tablet-side command in a POSIX shell')
@pytest.mark.parametrize('other', ['', 'ssh-ed25519 OTHER other-device\n'])
def test_remove_key_handles_last_key_and_preserves_others(tmp_path, other):
    import shlex
    import subprocess

    directory = tmp_path / '.ssh'
    directory.mkdir()
    authorized = directory / 'authorized_keys'
    authorized.write_text('ssh-ed25519 ABC+/= rephemeral\n' + other)
    device = MagicMock()
    config.remove_key(device, 'ssh-ed25519 ABC+/= rephemeral')
    command = device.check.call_args.args[0].replace('~/.ssh', shlex.quote(str(directory)))
    subprocess.run(['/bin/sh', '-c', command], check=True)
    assert authorized.read_text() == other
    assert authorized.stat().st_mode & 0o777 == 0o600


def test_device_paths_never_carry_a_backslash(tmp_path):
    """Host paths are Windows paths on Windows; device paths must stay POSIX.

    Runs a capture, an apply, a restore and a batch restore against a
    simulated tablet, with the host backup root under tmp_path, and checks
    every string handed to the device.
    """
    raw = io.BytesIO()
    Image.new('RGB', (4, 4), 'white').save(raw, format='PNG')
    stock = raw.getvalue()
    stock_sha = hashlib.sha256(stock).hexdigest()
    device = MagicMock()
    device.exists.side_effect = lambda path: not path.endswith('manifest.json')
    device.read_bytes.return_value = stock
    device.sha256.return_value = stock_sha
    device.write_bytes.side_effect = lambda path, data: hashlib.sha256(data).hexdigest()

    store = BackupStore(device, '20260827113527', host_root=tmp_path)
    applier = Applier(device, store)
    screen = screens.get('suspended')
    applier.apply(screen, stock, source=str(tmp_path / 'mixed/sep\\image.png'))
    applier.restore(screen)
    applier.restore_all(screens.SCREENS)

    strings = [arg for call in device.mock_calls for arg in call.args if isinstance(arg, str)]
    assert any(s.startswith('/home/root/.rephemeral/backups/') for s in strings)
    assert all('\\' not in s for s in strings), [s for s in strings if '\\' in s]
    for call in device.mock_calls:
        if call[0] in ('exists', 'read_bytes', 'write_bytes', 'write_home_bytes', 'sha256'):
            assert call.args[0].startswith('/'), call


def test_command_disconnect_is_normalized_and_channel_closed():
    device = Device()
    device._client = MagicMock()
    output = MagicMock()
    output.read.side_effect = OSError('unplugged')
    device._client.exec_command.return_value = (MagicMock(), output, MagicMock())
    with pytest.raises(DeviceError, match='unplugged'):
        device.run('uname -n')
    output.channel.close.assert_called_once()
