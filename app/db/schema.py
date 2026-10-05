"""Conexión a SQLite, creación de tablas y migraciones simples."""

from collections.abc import Callable
from pathlib import Path

from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

import app.models  # noqa: F401  (registra las tablas en SQLModel.metadata)

SCHEMA_VERSION = 5


def _v2_whatsapp_confidence(conn: Connection) -> None:
    """Fase 3: confianza del WhatsApp y aviso de botón roto."""
    conn.execute(text("ALTER TABLE businesses ADD COLUMN whatsapp_confidence VARCHAR(5)"))
    conn.execute(text("ALTER TABLE businesses ADD COLUMN whatsapp_broken_button BOOLEAN NOT NULL DEFAULT 0"))
    conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_businesses_whatsapp_confidence ON businesses (whatsapp_confidence)")
    )


def _v3_daily_leads(conn: Connection) -> None:
    """Fase 4 (leads diarios): confirmación manual del WhatsApp, descarte y leads por búsqueda."""
    for statement in (
        "ALTER TABLE businesses ADD COLUMN whatsapp_check VARCHAR(9)",
        "ALTER TABLE businesses ADD COLUMN whatsapp_checked_at DATETIME",
        "ALTER TABLE businesses ADD COLUMN discarded_at DATETIME",
        "ALTER TABLE businesses ADD COLUMN discard_reason VARCHAR",
        "CREATE INDEX IF NOT EXISTS ix_businesses_discarded_at ON businesses (discarded_at)",
        "ALTER TABLE searches ADD COLUMN leads_found INTEGER NOT NULL DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS ix_searches_created_at ON searches (created_at)",
    ):
        conn.execute(text(statement))


def _v4_findings(conn: Connection) -> None:
    """Fase 5: hallazgos automáticos (sin IA) de cada análisis."""
    conn.execute(text("ALTER TABLE website_analyses ADD COLUMN findings JSON NOT NULL DEFAULT '[]'"))


def _v5_no_repeats(conn: Connection) -> None:
    """No repetir leads: repetidos marcados. Las tablas nuevas (contactados y huellas) las crea create_all."""
    conn.execute(text("ALTER TABLE businesses ADD COLUMN duplicate_of_id INTEGER"))
    conn.execute(text("ALTER TABLE businesses ADD COLUMN duplicate_reason VARCHAR"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_businesses_duplicate_of_id ON businesses (duplicate_of_id)"))


# versión → función que lleva la base de la versión anterior a esa versión.
MIGRATIONS: dict[int, Callable[[Connection], None]] = {
    2: _v2_whatsapp_confidence,
    3: _v3_daily_leads,
    4: _v4_findings,
    5: _v5_no_repeats,
}


def create_db_engine(db_path: Path | str) -> Engine:
    """Motor SQLite. `":memory:"` crea una base temporal (para tests)."""
    connect_args = {"check_same_thread": False}
    if str(db_path) == ":memory:":
        # Una sola conexión compartida: si no, cada conexión vería una base vacía distinta.
        engine = create_engine("sqlite://", connect_args=connect_args, poolclass=StaticPool)
    else:
        engine = create_engine(f"sqlite:///{Path(db_path)}", connect_args=connect_args)

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    """Crea las tablas que falten y aplica las migraciones pendientes."""
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"))
        current = conn.execute(text("SELECT MAX(version) FROM schema_version")).scalar()
        if current is None:
            # Base nueva: create_all ya la dejó en la última versión.
            conn.execute(text("INSERT INTO schema_version (version) VALUES (:v)"), {"v": SCHEMA_VERSION})
            return
        for version in range(current + 1, SCHEMA_VERSION + 1):
            MIGRATIONS[version](conn)
            conn.execute(text("INSERT INTO schema_version (version) VALUES (:v)"), {"v": version})
