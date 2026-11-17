"""Testes do grafo do agente com um modelo falso (não gasta cota nem precisa de chave)."""

from datetime import datetime

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from app import schemas
from app import services as sv
from app.agente.grafo import Agente, construir_grafo
from app.agente.intencoes import RegistrarAusencia, CriarTarefa, resolver_periodo
from app.models import Alocacao, Tarefa

from .conftest import em, nova_tarefa, novo_func

AGORA = datetime(2026, 11, 15, 10, 0)  # domingo
TEC = {"papel": "tecnico", "quantidade": 1, "especialidades": [{"nome": "Elétrica", "nivel_min": 3}]}


class ModeloFalso:
    """Devolve as intenções programadas, na ordem, e guarda o que recebeu."""

    def __init__(self):
        self.fila: list = []
        self.recebidas: list = []

    def responde(self, nome, **args):
        self.fila.append((nome, args))

    def __call__(self, mensagens):
        self.recebidas.append(mensagens)
        item = self.fila.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def llm():
    return ModeloFalso()


@pytest.fixture
def agente(llm):
    return Agente(construir_grafo(llm, InMemorySaver(), agora=lambda: AGORA))


def opcoes(r):
    return [valor for valor, _ in r.pendente["opcoes"]]


def tarefas(db):
    db.expire_all()
    return db.scalars(select(Tarefa)).all()


def pedir_tarefa(llm, **extra):
    llm.responde("CriarTarefa", titulo="Visita técnica", data_inicio="2026-11-17",
                 equipe=[{"papel": "Técnico", "especialidades": ["eletrica:4"]}], **extra)


def test_criar_tarefa_so_grava_depois_da_aprovacao(db, llm, agente):
    novo_func(db, "Ana", esp=(("Elétrica", 5),), anos_experiencia=8)
    novo_func(db, "Beto", esp=(("Elétrica", 3),))
    pedir_tarefa(llm)

    r = agente.enviar("c1", "preciso de um técnico bom de elétrica terça")
    assert r.pendente and "Ana" in r.pendente["texto"] and "Beto" not in r.pendente["texto"]
    assert opcoes(r) == ["criar_confirmar", "so_criar", "descartar"]
    assert tarefas(db) == []  # nada gravado ainda

    contexto = llm.recebidas[0][0].content
    assert "domingo, 2026-11-15" in contexto and "Ana" in contexto and "tecnico" in contexto

    r2 = agente.decidir("c1", r.pendente["id"], "criar_confirmar")
    assert "Ana" in r2.mensagens[0] and r2.pendente is None
    [t] = tarefas(db)
    assert t.status == "alocada" and (t.inicio, t.fim) == (em(1, 8), em(1, 17))


def test_descartar_nao_grava_nada(db, llm, agente):
    novo_func(db, "Ana")
    pedir_tarefa(llm)
    r = agente.enviar("c1", "...")
    r2 = agente.decidir("c1", r.pendente["id"], "descartar")
    assert "descartado" in r2.mensagens[0].lower()
    assert tarefas(db) == []


def test_sim_digitado_confirma_sem_chamar_o_modelo(db, llm, agente):
    novo_func(db, "Ana", esp=(("Elétrica", 5),))
    pedir_tarefa(llm)
    agente.enviar("c1", "...")
    r = agente.enviar("c1", "Sim!")
    assert len(llm.recebidas) == 1
    assert "Ana" in r.mensagens[0] and tarefas(db)[0].status == "alocada"


def test_nova_mensagem_descarta_pedido_pendente(db, llm, agente):
    novo_func(db, "Ana", esp=(("Elétrica", 5),))
    pedir_tarefa(llm)
    agente.enviar("c1", "...")
    llm.responde("Responder", texto="Oi! Em que posso ajudar?")
    r = agente.enviar("c1", "oi")
    assert "descartado" in r.mensagens[0] and r.mensagens[-1] == "Oi! Em que posso ajudar?"
    assert agente.pendente("c1") is None and tarefas(db) == []


def test_botao_de_pedido_antigo_e_ignorado(db, llm, agente):
    novo_func(db, "Ana", esp=(("Elétrica", 5),))
    pedir_tarefa(llm)
    r = agente.enviar("c1", "...")
    assert agente.decidir("c1", "outro-id", "criar_confirmar") is None
    assert agente.decidir("outra-conversa", r.pendente["id"], "criar_confirmar") is None
    assert tarefas(db) == []


def test_ausencia_sugere_e_aplica_substituto(db, llm, agente):
    ana = novo_func(db, "Ana", esp=(("Elétrica", 5),), anos_experiencia=10)
    beto = novo_func(db, "Beto", esp=(("Elétrica", 4),))
    t = nova_tarefa(db, [TEC], titulo="Subestação")
    sv.gerar_proposta(db, t)
    sv.confirmar(db, t)

    llm.responde("RegistrarAusencia", funcionario_id=ana.id, data_inicio="2026-11-17", motivo="atestado")
    r = agente.enviar("c1", "a Ana está de atestado terça")
    assert "Isso atinge" in r.pendente["texto"] and f"#{t.id}" in r.pendente["texto"]

    r2 = agente.decidir("c1", r.pendente["id"], "registrar")
    assert "registrada" in r2.mensagens[0]
    assert opcoes(r2)[0] == f"trocar:{beto.id}" and "Beto" in r2.pendente["texto"]

    r3 = agente.decidir("c1", r2.pendente["id"], f"trocar:{beto.id}")
    assert "Beto entrou no lugar de Ana" in r3.mensagens[0] and r3.pendente is None
    db.expire_all()
    status = {a.funcionario_id: a.status for a in db.scalars(select(Alocacao))}
    assert status == {ana.id: "cancelada", beto.id: "confirmada"}


def test_tirar_da_tarefa_sem_substituto_viavel(db, llm, agente):
    ana = novo_func(db, "Ana")
    t = nova_tarefa(db, [TEC])
    sv.gerar_proposta(db, t)
    sv.confirmar(db, t)

    llm.responde("TirarDaTarefa", tarefa_id=t.id, funcionario_id=ana.id)
    r = agente.enviar("c1", "tira a Ana da tarefa 1")
    assert opcoes(r) == ["retirar", "manter"] and r.pendente["sim"] is None
    assert "Nenhum substituto viável" in r.pendente["texto"]

    r2 = agente.decidir("c1", r.pendente["id"], "retirar")
    assert "saiu" in r2.mensagens[0]
    assert tarefas(db)[0].status == "aberta"


def test_consulta_nao_pede_aprovacao(db, llm, agente):
    novo_func(db, "Ana")
    beto = novo_func(db, "Beto")
    sv.criar_indisponibilidade(db, beto, schemas.IndisponibilidadeIn(inicio=em(1, 0), fim=em(2, 0), motivo="folga"))
    llm.responde("ConsultarDisponibilidade", data_inicio="2026-11-17", papel="tecnico")
    r = agente.enviar("c1", "quem está livre terça?")
    assert r.pendente is None
    assert "Ana" in r.mensagens[0] and "Beto" not in r.mensagens[0]


def test_funcao_desconhecida_vira_mensagem_clara(db, llm, agente):
    novo_func(db, "Ana")
    llm.responde("CriarTarefa", titulo="x", data_inicio="2026-11-17", equipe=[{"papel": "astronauta"}])
    r = agente.enviar("c1", "...")
    assert r.pendente is None and "não conheço a função 'astronauta'" in r.mensagens[0]


def test_erro_de_cota_do_modelo(db, llm, agente):
    llm.fila.append(RuntimeError("429 RESOURCE_EXHAUSTED"))
    r = agente.enviar("c1", "oi")
    assert "limite gratuito" in r.mensagens[0]


def test_erro_de_projeto_sem_creditos(db, llm, agente):
    llm.fila.append(RuntimeError("402 RESOURCE_EXHAUSTED. Your prepayment credits are depleted."))
    r = agente.enviar("c1", "oi")
    assert "sem créditos" in r.mensagens[0] and "SEM faturamento" in r.mensagens[0]


def test_historico_fica_limitado(db, llm, agente):
    for i in range(15):
        llm.responde("Responder", texto=f"resposta {i}")
        agente.enviar("c1", f"mensagem {i}")
    estado = agente.grafo.get_state(agente._cfg("c1")).values
    assert len(estado["messages"]) <= 21
    assert estado["messages"][-1].content == "resposta 14"


def test_resolver_periodo():
    base = {"titulo": "x", "equipe": [{"papel": "tecnico"}]}
    assert resolver_periodo(RegistrarAusencia(funcionario_id=1, data_inicio="2026-11-17"), dia_inteiro=True) == (
        em(1, 0), em(2, 0))
    assert resolver_periodo(CriarTarefa(data_inicio="2026-11-17", **base)) == (em(1, 8), em(1, 17))
    p = CriarTarefa(data_inicio="2026-11-17", hora_inicio="14h30", hora_fim="18h", **base)
    assert resolver_periodo(p) == (em(1, 14).replace(minute=30), em(1, 18))
    with pytest.raises(ValueError):
        resolver_periodo(CriarTarefa(data_inicio="2026-11-17", hora_inicio="18:00", hora_fim="08:00", **base))
