import os

# antes de importar o app: banco em memória e nada vindo do .env local que mude o comportamento
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SECRET_KEY"] = "chave-de-teste"
os.environ["PAINEL_SENHA"] = ""
os.environ["API_TOKEN"] = ""
os.environ["APP_TIMEZONE"] = "America/Sao_Paulo"

from datetime import datetime, timedelta  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import models  # noqa: E402,F401
from app import schemas  # noqa: E402
from app import services as sv  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402

SEG = datetime(2026, 11, 16)  # uma segunda-feira


def em(dias: int, hora: int, minuto: int = 0) -> datetime:
    return SEG + timedelta(days=dias, hours=hora, minutes=minuto)


@pytest.fixture(autouse=True)
def _banco_limpo():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture(autouse=True)
def _agora_fixo(monkeypatch):
    """O "agora" dos serviços fica no domingo antes da semana dos testes (avisos não expiram)."""
    fixo = lambda: datetime(2026, 11, 15, 10, 0)  # noqa: E731
    monkeypatch.setattr(sv, "agora", fixo)
    monkeypatch.setattr(sv, "hoje", lambda: fixo().date())


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def csrf(client) -> str:
    """Token do formulário: o painel põe no cookie `csrf` na primeira visita."""
    if not client.cookies.get("csrf"):
        client.get("/saude")
        client.get("/login")
    return client.cookies.get("csrf")


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
