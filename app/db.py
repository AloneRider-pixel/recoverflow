from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class Base(DeclarativeBase):
    pass

def init_db():
    # Called only after all SQLAlchemy models have been imported.
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    if "invoices" in inspector.get_table_names():
        columns = {c["name"] for c in inspector.get_columns("invoices")}
        statements = []
        if "owner_id" not in columns: statements.append("ALTER TABLE invoices ADD COLUMN owner_id INTEGER")
        if "gstin" not in columns: statements.append("ALTER TABLE invoices ADD COLUMN gstin VARCHAR(30)")
        if "place_of_supply" not in columns: statements.append("ALTER TABLE invoices ADD COLUMN place_of_supply VARCHAR(80)")
        if "tax_rate" not in columns: statements.append("ALTER TABLE invoices ADD COLUMN tax_rate NUMERIC(6,2)")
        if "tax_amount" not in columns: statements.append("ALTER TABLE invoices ADD COLUMN tax_amount NUMERIC(14,2)")
        if "tds_amount" not in columns: statements.append("ALTER TABLE invoices ADD COLUMN tds_amount NUMERIC(14,2)")
        if statements:
            with engine.begin() as conn:
                for statement in statements: conn.execute(text(statement))

def get_db():
    db=SessionLocal()
    try:
        yield db
    finally:
        db.close()