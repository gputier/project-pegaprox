"""Rolling an update back restores the files of the backup, each to the path it came from.

The rollback copied the first .py at the root of the backup, the launcher, over
pegaprox/api/settings.py, and it looked for index.html at the root of the backup, where the
update never puts it. The operator saw a server that no longer started.

The route runs for real here, against a temp install. The restart is captured, not run.
"""
import os
import shutil
import threading
import types
from pathlib import Path

import pytest

import pegaprox.api.settings as settings

BACKUP = 'pegaprox_1.0.0_20260101_120000'
OLD = {  # what the backup holds: the release that was running before the update
    'pegaprox_multi_cluster.py': '# OLD launcher\n',
    'version.json': '{"version": "1.0.0"}\n',
    'requirements.txt': 'flask\n',
    'pegaprox/__init__.py': '# OLD package\n',
    'pegaprox/api/settings.py': '# OLD settings module\n',
    'pegaprox/core/manager.py': '# OLD manager\n',
    'web/index.html': '<html>OLD page</html>\n',
    'web/assets/app.js': '// OLD app\n',
    'static/logo.txt': 'OLD logo\n',
}
NEW = {rel: text.replace('OLD', 'NEW') for rel, text in OLD.items()}  # the release the update installed


def _write(root, files):
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


def _tree(root):
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            out[os.path.relpath(p, root)] = Path(p).read_bytes()
    return out


def _back_up_like_the_updater(install, backup):
    # the same copies the update route makes before it replaces anything
    for item in ['pegaprox', 'web', 'static']:
        if (install / item).is_dir():
            shutil.copytree(install / item, backup / item, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for f in ['pegaprox_multi_cluster.py', 'version.json', 'requirements.txt']:
        if (install / f).exists():
            shutil.copy2(install / f, backup / f)


@pytest.fixture
def box(tmp_path, monkeypatch):
    install = tmp_path / 'install'
    config = tmp_path / 'config'
    backup = config / 'backups' / BACKUP
    backup.mkdir(parents=True)
    _write(install, OLD)
    _back_up_like_the_updater(install, backup)  # the backup is taken from the old release
    _write(install, NEW)  # then the update installs the new release over it
    monkeypatch.setattr(settings, '__file__', str(install / 'pegaprox' / 'api' / 'settings.py'))
    monkeypatch.setattr(settings, 'CONFIG_DIR', str(config))
    started = []

    class _Thread:
        # the restart thread is recorded, never run: a real restart would end the test process
        def __init__(self, target=None, **kw):
            self.target = target

        def start(self):
            started.append(self.target)
    monkeypatch.setattr(threading, 'Thread', _Thread)
    return types.SimpleNamespace(install=install, backup=backup, started=started)


def _rollback(api, seed, backup_name=BACKUP):
    admin = api.as_user(seed.user('root', role='admin'))
    return admin.post('/api/pegaprox/update/rollback', json={'backup': backup_name})


def test_the_launcher_comes_back_as_the_launcher(api, seed, box):
    """After the rollback the server starts from the old launcher, at the install root."""
    r = _rollback(api, seed)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()['success'] is True
    assert (box.install / 'pegaprox_multi_cluster.py').read_text() == OLD['pegaprox_multi_cluster.py']
    assert len(box.started) == 1


def test_the_settings_module_is_not_overwritten_by_the_launcher(api, seed, box):
    """The API still loads its own settings module after the rollback, not a copy of the launcher."""
    assert _rollback(api, seed).status_code == 200
    settings_src = (box.install / 'pegaprox' / 'api' / 'settings.py').read_text()
    assert settings_src == OLD['pegaprox/api/settings.py']
    assert settings_src != OLD['pegaprox_multi_cluster.py']


def test_every_file_the_backup_holds_goes_back_to_its_path(api, seed, box):
    """Each file of the old release is back where it was, with the content it had."""
    assert _rollback(api, seed).status_code == 200
    for rel, text in OLD.items():
        assert (box.install / rel).read_text() == text, rel
    assert _tree(box.install) == {rel: text.encode() for rel, text in OLD.items()}


def test_the_page_comes_back_under_web(api, seed, box):
    """The page of the old release is back in web/, and no index.html is left beside the API."""
    assert _rollback(api, seed).status_code == 200
    assert (box.install / 'web' / 'index.html').read_text() == OLD['web/index.html']
    assert not (box.install / 'pegaprox' / 'api' / 'index.html').exists()


@pytest.mark.parametrize('contents', [{}, {'web/index.html': '<html>only the page</html>\n'},
                                      {'pegaprox_multi_cluster.py': '# launcher alone\n'},
                                      {'pegaprox/api/settings.py': '# settings alone\n'}],
                         ids=['empty', 'page-only', 'launcher-only', 'settings-only'])
def test_a_backup_with_no_install_in_it_is_refused_and_changes_nothing(api, seed, box, contents):
    """A backup that does not hold a whole install gets an error, and the install stays as it was."""
    shutil.rmtree(box.backup)
    box.backup.mkdir()
    _write(box.backup, contents)
    before = _tree(box.install)
    r = _rollback(api, seed)
    assert r.status_code == 400, r.get_data(as_text=True)
    assert 'error' in r.get_json() and 'success' not in r.get_json()
    assert _tree(box.install) == before
    assert box.started == []


def test_the_answer_names_what_came_back(api, seed, box):
    """The answer lists 'frontend' when the backup holds a page, and 'backend' alone when it does not."""
    assert _rollback(api, seed).get_json()['restored'] == ['backend', 'frontend']
    shutil.rmtree(box.backup / 'web')
    assert _rollback(api, seed).get_json()['restored'] == ['backend']


def test_a_file_only_the_newer_release_has_is_left_in_place(api, seed, box):
    """Known limit: the rollback copies the backup back, it does not delete what the update added."""
    _write(box.install, {'pegaprox/api/added_by_the_update.py': '# NEW only\n'})
    assert _rollback(api, seed).status_code == 200
    assert (box.install / 'pegaprox' / 'api' / 'added_by_the_update.py').read_text() == '# NEW only\n'
    assert (box.install / 'pegaprox' / 'api' / 'settings.py').read_text() == OLD['pegaprox/api/settings.py']
