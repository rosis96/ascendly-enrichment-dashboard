"""Database setup (SQLite for local dev; swap DASHBOARD_DB_URL for Postgres later)."""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

DB_URL = os.getenv("DASHBOARD_DB_URL", f"sqlite:///{os.path.join(DATA_DIR, 'app.db')}")

# Managed Postgres providers (Railway, Neon, Supabase) often hand out a
# "postgres://" URL, which SQLAlchemy 2.0 rejects. Normalize it.
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

if DB_URL.startswith("sqlite"):
    engine = create_engine(DB_URL, connect_args={"check_same_thread": False, "timeout": 30}, future=True)
else:
    # bigger pool so many concurrent workers can each grab a short-lived connection.
    # Workers hold connections only briefly (read, then release during scrape, then
    # write), so this comfortably supports ~100 classify workers.
    engine = create_engine(DB_URL, pool_size=30, max_overflow=30, pool_pre_ping=True,
                           pool_timeout=60, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def _migrate():
    """Best-effort add of columns introduced after a db was first created, so an
    existing dev db keeps working without being deleted."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    wanted = {
        "leads": [("verify", "JSON"), ("email_status", "VARCHAR"), ("industry", "VARCHAR"),
                  ("title_status", "VARCHAR"), ("esp", "VARCHAR"),
                  ("employees", "INTEGER"), ("country", "VARCHAR"), ("state", "VARCHAR"),
                  ("seniority", "VARCHAR"),
                  ("icp_decision", "VARCHAR"), ("icp_score", "INTEGER"), ("icp_confidence", "INTEGER"),
                  ("verify_source", "VARCHAR"), ("free_status", "VARCHAR")],
        "jobs": [("kind", "VARCHAR"), ("summary", "JSON")],
    }
    with engine.begin() as conn:
        for table, cols in wanted.items():
            if not insp.has_table(table):
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for name, typ in cols:
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {typ}"))


def init_db():
    import models  # noqa: F401  (register mappers)
    Base.metadata.create_all(engine)
    _migrate()
