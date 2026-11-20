"""Load only the project's explicit .env, independent of the shell directory."""
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / '.env'


def load_environment(path=None):
    # Real process variables retain precedence for deployments and one-off runs.
    # utf-8-sig also accepts files saved with a BOM by Windows editors.
    return load_dotenv(path or ENV_FILE, override=False, encoding='utf-8-sig')


def database_url():
    from sqlalchemy.engine import make_url
    raw = os.getenv('DATABASE_URL') or 'sqlite:///backend/compliance.db'
    parsed = make_url(raw)
    if parsed.get_backend_name() == 'sqlite' and parsed.database not in (None, '', ':memory:'):
        path = Path(parsed.database)
        # Relative SQLite locations are relative to the project, not cwd.
        if not path.is_absolute() and not parsed.query.get('uri'):
            parsed = parsed.set(database=str((PROJECT_ROOT / path).resolve()))
    return parsed.render_as_string(hide_password=False)
