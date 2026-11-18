"""Forçar pelo bot: incluir ou trocar quem não cumpre as regras, com motivo na auditoria."""

from datetime import date, datetime, timedelta

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app import schemas
from app import services as sv
from app.agente.grafo import Agente, construir_grafo
from app.agente.intencoes import POR_NOME
from app.models import Auditoria

from .conftest import em, nova_tarefa, novo_func
from .test_agente import TEC, ModeloFalso, opcoes
from .test_agente_novo import status_alocacoes

AGORA = datetime(2026, 11, 15, 10, 0)  # domingo
NR10 = {**TEC, "certificacoes": ["NR10"]}


@pytest.fixture
def llm():
    return ModeloFalso()


@pytest.fixture
def agente(llm):
    return Agente(construir_grafo(llm, InMemorySaver(), agora=lambda: AGORA))


def auditoria(db, acao):
    db.expire_all()
    return db.query(Auditoria).filter(Auditoria.acao == acao).all()


def test_incluir_quem_cumpre_as_regras_confirma_sem_forcar(db, llm, agente):
    beto = novo_func(db, "Beto")
    t = nova_tarefa(db, [NR10])
    llm.responde("IncluirNaTarefa", tarefa_id=t.id, funcionario_id=beto.id)
    r = agente.enviar("c1", "coloca o Beto na tarefa 1")
    assert opcoes(r) == ["incluir", "descartar"]
    r = agente.enviar("c1", "sim")
    assert "Beto" in r.mensagens[0] and "Forçado" not in r.mensagens[0]
    assert status_alocacoes(db) == {"Beto": "confirmada"} and not auditoria(db, "forcar")


def test_forcar_inclusao_com_certificacao_vencida_pede_motivo_e_audita(db, llm, agente):
    vencida = date(2026, 11, 1)
    helena = novo_func(db, "Helena", cert=(("NR10", vencida),))
    t = nova_tarefa(db, [NR10])
    llm.responde("IncluirNaTarefa", tarefa_id=t.id, funcionario_id=helena.id)
    r = agente.enviar("c1", "põe a Helena na tarefa 1 mesmo assim")
    assert opcoes(r) == ["forcar", "descartar"] and "NR10" in r.pendente["texto"]

    r = agente.decidir("c1", r.pendente["id"], "forcar")
    assert r.pendente["texto_livre"] and "motivo" in r.pendente["texto"].lower()
    assert status_alocacoes(db) == {}  # nada gravado antes do motivo

    r = agente.enviar("c1", "renovação da NR10 agendada para segunda, cliente autorizou")
    assert r.pendente is None and "Forçado contra as regras" in r.mensagens[-1]
    assert status_alocacoes(db) == {"Helena": "confirmada"}
    [reg] = auditoria(db, "forcar")
    assert reg.autor == "telegram:c1" and reg.funcionario_id == helena.id
    assert reg.detalhes["motivo"] == "renovação da NR10 agendada para segunda, cliente autorizou"
    assert any("NR10" in v for v in reg.detalhes["violacoes"])
    a = t.alocacoes[0]
    db.refresh(a)
    assert a.justificativa["forcado"] and a.justificativa["forcado_por"] == "telegram:c1"


def test_sim_digitado_nao_forca(db, llm, agente):
    sem = novo_func(db, "Caio", cert=())
    t = nova_tarefa(db, [NR10])
    llm.responde("IncluirNaTarefa", tarefa_id=t.id, funcionario_id=sem.id)
    r = agente.enviar("c1", "coloca o Caio na 1")
    assert r.pendente["sim"] is None
    llm.responde("Responder", texto="Ok.")
    r = agente.enviar("c1", "sim")
    assert "descartado" in r.mensagens[0]  # "sim" vira mensagem nova; o pedido de forçar é descartado
    assert status_alocacoes(db) == {}


def test_cancelar_no_motivo_nao_grava(db, llm, agente):
    sem = novo_func(db, "Caio", cert=())
    t = nova_tarefa(db, [NR10])
    llm.responde("IncluirNaTarefa", tarefa_id=t.id, funcionario_id=sem.id)
    r = agente.enviar("c1", "coloca o Caio na 1")
    agente.decidir("c1", r.pendente["id"], "forcar")
    r = agente.enviar("c1", "cancela")
    assert "descartado" in r.mensagens[0] and r.pendente is None
    assert status_alocacoes(db) == {} and not auditoria(db, "forcar")


def test_forcar_troca(db, llm, agente):
    ana, caio = novo_func(db, "Ana"), novo_func(db, "Caio", cert=())
    t = nova_tarefa(db, [NR10])
    sv.confirmar(db, t, [schemas.Escolha(requisito_id=t.requisitos[0].id, funcionario_id=ana.id)])
    llm.responde("SubstituirNaTarefa", tarefa_id=t.id, sai_id=ana.id, entra_id=caio.id)
    r = agente.enviar("c1", "coloca o Caio no lugar da Ana")
    r = agente.decidir("c1", r.pendente["id"], "forcar")
    r = agente.enviar("c1", "só ele conhece o cliente")
    assert "Caio entrou no lugar de Ana" in r.mensagens[-1]
    assert status_alocacoes(db) == {"Ana": "cancelada", "Caio": "confirmada"}
    assert auditoria(db, "forcar")[0].detalhes["motivo"] == "só ele conhece o cliente"
    assert len(auditoria(db, "substituir")) == 1


def test_problema_novo_depois_da_previa_nao_e_forcado(db, llm, agente):
    caio = novo_func(db, "Caio", cert=())
    t = nova_tarefa(db, [NR10])
    llm.responde("IncluirNaTarefa", tarefa_id=t.id, funcionario_id=caio.id)
    r = agente.enviar("c1", "coloca o Caio na 1")
    agente.decidir("c1", r.pendente["id"], "forcar")
    # antes do motivo, o Caio fica ausente no dia: o gestor não viu essa violação
    sv.criar_indisponibilidade(db, caio, schemas.IndisponibilidadeIn(inicio=em(1, 0), fim=em(2, 0), motivo="atestado"))
    r = agente.enviar("c1", "precisamos dele")
    assert "Nada foi gravado" in r.mensagens[-1] and "atestado" in r.mensagens[-1]
    assert status_alocacoes(db) == {}


def test_escolha_da_vaga(db, llm, agente):
    eng = novo_func(db, "Eva", papel="engenheiro")
    beto = novo_func(db, "Beto")
    t = nova_tarefa(db, [TEC, {"papel": "engenheiro", "quantidade": 1}])
    # sem dizer a função, entra na vaga da função da pessoa
    llm.responde("IncluirNaTarefa", tarefa_id=t.id, funcionario_id=eng.id)
    r = agente.enviar("c1", "coloca a Eva na 1")
    assert "como engenheiro" in r.pendente["texto"]
    agente.descartar("c1")
    # função pedida que não tem vaga aberta
    llm.responde("IncluirNaTarefa", tarefa_id=t.id, funcionario_id=beto.id, papel="supervisor")
    r = agente.enviar("c1", "coloca o Beto como supervisor na 1")
    assert r.pendente is None and "não tem vaga aberta de 'supervisor'" in r.mensagens[0]


def test_equipe_completa_pede_troca(db, llm, agente):
    ana, beto = novo_func(db, "Ana"), novo_func(db, "Beto")
    t = nova_tarefa(db, [TEC])
    sv.confirmar(db, t, [schemas.Escolha(requisito_id=t.requisitos[0].id, funcionario_id=ana.id)])
    llm.responde("IncluirNaTarefa", tarefa_id=t.id, funcionario_id=beto.id)
    r = agente.enviar("c1", "coloca o Beto na 1")
    assert "já está completa" in r.mensagens[0] and "quem sai" in r.mensagens[0]


def test_intencao_nova_esta_registrada():
    assert "IncluirNaTarefa" in POR_NOME
