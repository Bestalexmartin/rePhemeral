"""Where files live, and carrying an old install over without losing any."""
import hashlib
import json
from pathlib import Path

import pytest

from rephemeral import backup, config, migrate, paths


def test_windows_keeps_config_and_backups_together_in_localappdata():
    home = Path('home')
    config_dir, data_dir = paths.default_dirs('nt', {'LOCALAPPDATA': 'local'}, home)
    assert config_dir == data_dir == Path('local') / 'rephemeral'


def test_windows_without_localappdata_uses_its_usual_place():
    home = Path('home')
    config_dir, _ = paths.default_dirs('nt', {}, home)
    assert config_dir == home / 'AppData' / 'Local' / 'rephemeral'


def test_posix_layout_is_unchanged():
    home = Path('home')
    assert paths.default_dirs('posix', {}, home) == paths.legacy_dirs(home)
    assert paths.legacy_dirs(home) == (
        home / '.config' / 'rephemeral', home / '.local' / 'share' / 'rephemeral')


@pytest.mark.parametrize('os_name', ['nt', 'posix'])
def test_both_locations_can_be_overridden(os_name):
    environ = {'REPHEMERAL_CONFIG_DIR': 'c', 'REPHEMERAL_DATA_DIR': 'd', 'LOCALAPPDATA': 'x'}
    assert paths.default_dirs(os_name, environ, Path('home')) == (Path('c'), Path('d'))


def test_backups_live_under_the_data_directory():
    assert backup.HOST_BACKUP_ROOT == paths.DATA_DIR / 'backups' == paths.BACKUP_DIR


def snapshot(root):
    return {p.relative_to(root): p.read_bytes() for p in sorted(root.rglob('*')) if p.is_file()}


@pytest.fixture
def legacy_config(tmp_path):
    old = tmp_path / 'old-config'
    key = config.ensure_key(old / config.KEY_NAME)
    config.Config(key_path=str(key)).save(old / config.CONFIG_NAME)
    return old


def test_config_is_copied_restricted_and_repointed(tmp_path, legacy_config):
    before = snapshot(legacy_config)
    new = tmp_path / 'new' / 'rephemeral'
    report = migrate.Report()
    migrate.migrate_config(legacy_config, new, report)

    assert snapshot(legacy_config) == before
    assert (new / config.KEY_NAME).read_bytes() == (legacy_config / config.KEY_NAME).read_bytes()
    assert config.pub_path(new / config.KEY_NAME).is_file()
    assert config.key_exposure(new / config.KEY_NAME) == []
    assert config.load(new / config.CONFIG_NAME).key == new / config.KEY_NAME
    assert report.copied and not report.problems

    again = migrate.Report()
    migrate.migrate_config(legacy_config, new, again)
    assert again.copied == []


def test_config_never_overwrites_a_location_in_use(tmp_path, legacy_config):
    new = tmp_path / 'new'
    new.mkdir()
    (new / config.CONFIG_NAME).write_text('host = "10.0.0.9"\n')
    migrate.migrate_config(legacy_config, new, migrate.Report())
    assert config.load(new / config.CONFIG_NAME).host == '10.0.0.9'
    assert not (new / config.KEY_NAME).exists()


def test_a_key_kept_elsewhere_stays_where_it_is(tmp_path):
    old, elsewhere = tmp_path / 'old', tmp_path / 'elsewhere' / 'key'
    config.ensure_key(elsewhere)
    config.Config(key_path=str(elsewhere)).save(old / config.CONFIG_NAME)
    new = tmp_path / 'new'
    migrate.migrate_config(old, new, migrate.Report())
    assert config.load(new / config.CONFIG_NAME).key == elsewhere


def test_a_failed_config_copy_leaves_nothing_half_done(tmp_path, legacy_config, monkeypatch):
    new = tmp_path / 'new'
    monkeypatch.setattr(config, 'load', lambda path: (_ for _ in ()).throw(OSError('disk')))
    with pytest.raises(OSError):
        migrate.migrate_config(legacy_config, new, migrate.Report())
    assert not (new / config.KEY_NAME).exists()
    assert not (new / config.CONFIG_NAME).exists()


@pytest.fixture
def legacy_backups(tmp_path):
    old = tmp_path / 'old-backups'
    build = old / '20260827113527'
    build.mkdir(parents=True)
    screens = {}
    for key in ('suspended', 'poweroff'):
        data = f'stock {key}'.encode()
        (build / f'{key}.png').write_bytes(data)
        screens[key] = {'stock_sha256': hashlib.sha256(data).hexdigest(),
                        'backup_file': f'{key}.png', 'applied': []}
    (build / 'manifest.json').write_text(json.dumps({'schema': 1, 'screens': screens}))
    return old


def test_backups_are_copied_verified_and_left_in_place(tmp_path, legacy_backups):
    before = snapshot(legacy_backups)
    new = tmp_path / 'new-backups'
    report = migrate.Report()
    migrate.migrate_backups(legacy_backups, new, report)

    assert snapshot(legacy_backups) == before
    assert snapshot(new) == before
    assert report.copied == [f'3 backup file(s) from {legacy_backups} to {new}']
    assert report.problems == []

    again = migrate.Report()
    migrate.migrate_backups(legacy_backups, new, again)
    assert again.copied == [] and again.problems == []


def test_a_corrupt_backup_is_copied_and_reported(tmp_path, legacy_backups):
    (legacy_backups / '20260827113527' / 'poweroff.png').write_bytes(b'changed')
    report = migrate.Report()
    migrate.migrate_backups(legacy_backups, tmp_path / 'new', report)
    assert (tmp_path / 'new' / '20260827113527' / 'poweroff.png').read_bytes() == b'changed'
    assert len(report.problems) == 1 and 'poweroff.png' in report.problems[0]


def test_an_existing_backup_is_never_replaced(tmp_path, legacy_backups):
    new = tmp_path / 'new'
    (new / '20260827113527').mkdir(parents=True)
    (new / '20260827113527' / 'suspended.png').write_bytes(b'already here')
    (new / '20260827113527' / 'manifest.json').write_text('{"newer": true}')
    report = migrate.Report()
    migrate.migrate_backups(legacy_backups, new, report)
    assert (new / '20260827113527' / 'suspended.png').read_bytes() == b'already here'
    assert (new / '20260827113527' / 'manifest.json').read_text() == '{"newer": true}'
    assert len(report.problems) == 1 and 'suspended.png' in report.problems[0]
    assert not list(new.rglob('*.tmp'))


def test_same_location_is_a_no_op(tmp_path, legacy_config, legacy_backups):
    report = migrate.Report()
    migrate.migrate_config(legacy_config, legacy_config, report)
    migrate.migrate_backups(legacy_backups, legacy_backups, report)
    assert report == migrate.Report()


def test_overrides_skip_migration_and_leftovers(tmp_path, legacy_config, legacy_backups,
                                                 monkeypatch):
    old_data = tmp_path / 'old-data'
    old_data.mkdir()
    legacy_backups.rename(old_data / 'backups')
    monkeypatch.setattr(paths, 'legacy_dirs', lambda: (legacy_config, old_data))
    monkeypatch.setattr(paths, 'CONFIG_DIR', tmp_path / 'cfg')
    monkeypatch.setattr(paths, 'BACKUP_DIR', tmp_path / 'data' / 'backups')

    assert set(migrate.leftovers({})) == {'config', 'backups'}
    assert migrate.leftovers({'REPHEMERAL_CONFIG_DIR': 'x', 'REPHEMERAL_DATA_DIR': 'y'}) == {}

    migrate.run({'REPHEMERAL_DATA_DIR': 'y'})
    assert (tmp_path / 'cfg' / config.KEY_NAME).is_file()
    assert not (tmp_path / 'data').exists()
