from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class Base(DeclarativeBase):
    pass

def init_db():
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    if "invoices" in inspector.get_table_names():
        columns = {c["name"] for c in inspector.get_columns("invoices")}
        if "owner_id" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE invoices ADD COLUMN owner_id INTEGER"))

init_db()

def get_db():
    db=SessionLocal()
    try: yield db
    finally: db.close()
