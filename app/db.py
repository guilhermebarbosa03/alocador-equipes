from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import DATABASE_URL

_kwargs: dict = {}
_sqlite = DATABASE_URL.startswith("sqlite")
_memoria = DATABASE_URL in ("sqlite://", "sqlite:///:memory:")
if _sqlite:
    # timeout: espera o outro processo (painel x bot) liberar a escrita em vez de falhar na hora
    _kwargs["connect_args"] = {"check_same_thread": False, "timeout": 10}
    if _memoria:
        _kwargs["poolclass"] = StaticPool

engine = create_engine(DATABASE_URL, **_kwargs)

if _sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
        dbapi_conn.execute("PRAGMA busy_timeout=5000")
        if not _memoria:
            # WAL: leituras não bloqueiam escrita; painel e bot podem usar o mesmo arquivo ao mesmo tempo
            dbapi_conn.execute("PRAGMA journal_mode=WAL")


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Ajustes para bancos criados por versões anteriores (create_all só cria o que não existe).
_AJUSTES = [
    "CREATE INDEX IF NOT EXISTS ix_funcionarios_telegram_id ON funcionarios (telegram_id)",
]


def init_db() -> None:
    from . import models  # noqa: F401  (registra as tabelas)

    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for sql in _AJUSTES:
            conn.execute(text(sql))
