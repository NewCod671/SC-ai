from .config import database_url
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

class Base(DeclarativeBase):
    pass

url = database_url()
engine = create_engine(url, hide_parameters=True, connect_args={'check_same_thread': False} if url.startswith('sqlite') else {})
if url.startswith('sqlite'):
    @event.listens_for(engine, 'connect')
    def foreign_keys(connection, _):
        connection.execute('PRAGMA foreign_keys=ON')
SessionLocal = sessionmaker(engine, expire_on_commit=False)

def get_db():
    with SessionLocal() as db:
        yield db
