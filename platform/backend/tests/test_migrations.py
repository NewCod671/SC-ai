from alembic import command
from alembic.config import Config
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text, inspect
from app.db import Base
from pathlib import Path

def test_initial_migration_matches_models_and_downgrades(tmp_path, monkeypatch):
    url = 'sqlite:///'+str(tmp_path/'migration.db').replace('\\','/')
    monkeypatch.setenv('DATABASE_URL',url)
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend/'alembic.ini'))
    config.set_main_option('script_location',str(backend/'alembic'))
    command.upgrade(config,'head')
    engine = create_engine(url)
    with engine.connect() as connection:
        differences = compare_metadata(MigrationContext.configure(connection),Base.metadata)
        assert differences == []
    engine.dispose()
    command.downgrade(config,'base')

def test_skill_migration_preserves_legacy_data(tmp_path, monkeypatch):
    url = 'sqlite:///'+str(tmp_path/'legacy.db').replace('\\','/')
    monkeypatch.setenv('DATABASE_URL', url)
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend/'alembic.ini'))
    config.set_main_option('script_location', str(backend/'alembic'))
    command.upgrade(config, '0001')
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO skills (id, name, version, instructions) VALUES ('legacy', 'Review', '1', 'Keep evidence')"))
    command.upgrade(config, 'head')
    with engine.connect() as connection:
        assert connection.execute(text("SELECT instructions FROM skills WHERE id='legacy'")).scalar() == 'Keep evidence'
        assert {'skill_families', 'standard_skill_versions'} <= set(inspect(connection).get_table_names())
    engine.dispose()
