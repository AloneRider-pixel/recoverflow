from sqlalchemy import create_engine, inspect, text
from datetime import datetime, timedelta
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
        if "workspace_id" not in columns: statements.append("ALTER TABLE invoices ADD COLUMN workspace_id INTEGER")
        if statements:
            with engine.begin() as conn:
                for statement in statements: conn.execute(text(statement))
    user_columns = {c["name"] for c in inspector.get_columns("users")}
    user_statements = []
    if "trial_started_at" not in user_columns: user_statements.append("ALTER TABLE users ADD COLUMN trial_started_at TIMESTAMP")
    if "trial_ends_at" not in user_columns: user_statements.append("ALTER TABLE users ADD COLUMN trial_ends_at TIMESTAMP")
    if "workspace_id" not in user_columns: user_statements.append("ALTER TABLE users ADD COLUMN workspace_id INTEGER")
    if "role" not in user_columns: user_statements.append("ALTER TABLE users ADD COLUMN role VARCHAR(30) DEFAULT 'owner'")
    if user_statements:
        with engine.begin() as conn:
            for statement in user_statements: conn.execute(text(statement))

    # Backfill a workspace and membership for pre-enterprise accounts.
    from .models import User, Workspace, TeamMember, Invoice
    with SessionLocal.begin() as db:
        users=list(db.query(User).all())
        now=datetime.utcnow()
        for user in users:
            if not user.trial_started_at:
                user.trial_started_at=user.created_at or now
            if not user.trial_ends_at:
                start=user.trial_started_at or now
                user.trial_ends_at=start+timedelta(days=14)
            if not user.workspace_id:
                workspace=Workspace(name=user.company_name or "My Business")
                db.add(workspace); db.flush()
                user.workspace_id=workspace.id; user.role="owner"
            else:
                workspace=db.get(Workspace,user.workspace_id)
                if not workspace:
                    workspace=Workspace(name=user.company_name or "My Business")
                    db.add(workspace); db.flush()
                    user.workspace_id=workspace.id; user.role="owner"
        db.flush()
        for user in users:
            if user.workspace_id:
                membership=db.query(TeamMember).filter(TeamMember.workspace_id==user.workspace_id,TeamMember.user_id==user.id).first()
                if not membership:
                    db.add(TeamMember(workspace_id=user.workspace_id,user_id=user.id,role=user.role or "owner",status="active"))
        db.flush()
        for invoice in db.query(Invoice).filter(Invoice.workspace_id.is_(None)).all():
            if invoice.owner_id:
                owner=db.get(User,invoice.owner_id)
                invoice.workspace_id=owner.workspace_id if owner else None

def get_db():
    db=SessionLocal()
    try:
        yield db
    finally:
        db.close()