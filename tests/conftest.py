import os

os.environ["DATABASE_URL"] = "sqlite://"  # banco em memória, antes de importar o app

from datetime import datetime, timedelta  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import models  # noqa: E402,F401
from app import schemas  # noqa: E402
from app import services as sv  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402

SEG = datetime(2026, 11, 16)  # uma segunda-feira


def em(dias: int, hora: int) -> datetime:
    return SEG + timedelta(days=dias, hours=hora)


@pytest.fixture(autouse=True)
def _banco_limpo():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def novo_func(db, nome, papel="tecnico", esp=(("Elétrica", 4),), cert=(("NR10", None),), **kw):
    return sv.criar_funcionario(db, schemas.FuncionarioIn(
        nome=nome, papel=papel,
        especialidades=[{"nome": n, "nivel": nv} for n, nv in esp],
        certificacoes=[{"nome": n, "validade": v} for n, v in cert],
        **kw,
    ))


def nova_tarefa(db, requisitos, inicio=None, fim=None, **kw):
    return sv.criar_tarefa(db, schemas.TarefaIn(
        titulo=kw.pop("titulo", "Tarefa"), inicio=inicio or em(1, 8), fim=fim or em(1, 17),
        requisitos=requisitos, **kw,
    ))
