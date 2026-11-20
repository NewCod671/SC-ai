import os
import shutil
import subprocess
import sys
from pathlib import Path

from sqlalchemy.engine import make_url
from app import config


def test_dotenv_supports_bom_quotes_comments_and_existing_environment(tmp_path, monkeypatch):
    env_file = tmp_path / '.env'
    env_file.write_text("CONFIG_TEST_SECRET='test# key with spaces'\nCONFIG_TEST_MODE=file # comment\n", encoding='utf-8-sig')
    monkeypatch.delenv('CONFIG_TEST_SECRET', raising=False)
    monkeypatch.setenv('CONFIG_TEST_MODE', 'process')
    config.load_environment(env_file)
    assert os.environ['CONFIG_TEST_SECRET'] == 'test# key with spaces'
    assert os.environ['CONFIG_TEST_MODE'] == 'process'
    monkeypatch.delenv('CONFIG_TEST_SECRET')


def test_missing_dotenv_is_optional(tmp_path):
    assert config.load_environment(tmp_path / 'missing.env') is False


def test_package_startup_loads_explicit_project_env_not_cwd(tmp_path):
    project = tmp_path / 'project'
    package = project / 'backend' / 'app'
    package.mkdir(parents=True)
    for name in ('__init__.py', 'config.py'):
        shutil.copyfile(Path(config.__file__).parent / name, package / name)
    (project / '.env').write_text('CONFIG_TEST_MARKER=project\n', encoding='utf-8')
    other = tmp_path / 'other'
    other.mkdir()
    (other / '.env').write_text('CONFIG_TEST_MARKER=wrong_directory\n', encoding='utf-8')
    environment = dict(os.environ)
    environment.pop('CONFIG_TEST_MARKER', None)
    environment['PYTHONPATH'] = str(project / 'backend') + os.pathsep + environment.get('PYTHONPATH', '')
    result = subprocess.run([sys.executable, '-c',
        "import app, os; print(os.environ.get('CONFIG_TEST_MARKER'))"],
        cwd=other, env=environment, capture_output=True, text=True, timeout=15, check=True)
    assert result.stdout.strip() == 'project'


def test_sqlite_default_and_relative_path_point_at_same_existing_location_from_any_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    expected = config.PROJECT_ROOT / 'backend' / 'compliance.db'
    monkeypatch.chdir(tmp_path)
    assert Path(make_url(config.database_url()).database) == expected
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///backend/compliance.db')
    assert Path(make_url(config.database_url()).database) == expected


def test_absolute_sqlite_and_in_memory_urls_are_not_rebased(tmp_path, monkeypatch):
    url = 'sqlite:///' + str(tmp_path / 'test.db').replace('\\', '/')
    monkeypatch.setenv('DATABASE_URL', url)
    assert make_url(config.database_url()) == make_url(url)
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///:memory:')
    assert config.database_url() == 'sqlite:///:memory:'


def test_production_database_credentials_and_options_are_preserved(monkeypatch):
    url = 'postgresql+psycopg://demo:test%40password@db.example/compliance?sslmode=require'
    monkeypatch.setenv('DATABASE_URL', url)
    assert make_url(config.database_url()) == make_url(url)
