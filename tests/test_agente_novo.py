"""Agente: casos da revisão (aprovação que mudou, contexto, intenções novas, voz, modo equipe, travas)."""

import threading
import time
from datetime import date, datetime

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from app import schemas
from app import services as sv
from app.agente.equipe import AssistenteEquipe
from app.agente.grafo import Agente, construir_grafo
from app.agente.llm import montar_contexto
from app.models import Alocacao, SolicitacaoAusencia

from .conftest import em, nova_tarefa, novo_func
from .test_agente import TEC, ModeloFalso, opcoes, pedir_tarefa, tarefas

AGORA = datetime(2026, 11, 15, 10, 0)  # domingo


@pytest.fixture
def llm():
    return ModeloFalso()


@pytest.fixture
def agente(llm):
    return Agente(construir_grafo(llm, InMemorySaver(), agora=lambda: AGORA))


def status_alocacoes(db):
    db.expire_all()
    return {a.funcionario.nome: a.status for a in db.scalars(select(Alocacao))}


# ---------- 1.1: o gestor aprova uma equipe; se ela mudou, nada é confirmado calado ----------


def test_criar_tarefa_nao_troca_a_equipe_aprovada_em_silencio(db, llm, agente):
    ana = novo_func(db, "Ana", esp=(("Elétrica", 5),), anos_experiencia=10)
    novo_func(db, "Beto", esp=(("Elétrica", 4),))
    pedir_tarefa(llm)
    r = agente.enviar("c1", "preciso de um técnico terça")
    assert "Ana" in r.pendente["texto"]

    # entre a prévia e o clique, a Ana fica indisponível
    sv.criar_indisponibilidade(db, ana, schemas.IndisponibilidadeIn(inicio=em(1, 0), fim=em(2, 0), motivo="atestado"))
    r2 = agente.decidir("c1", r.pendente["id"], "criar_confirmar")
    assert "mudou desde a prévia" in r2.mensagens[0] and "Ana" in r2.mensagens[0]
    assert status_alocacoes(db) == {}  # ninguém confirmado sem o gestor ver
    assert r2.pendente and "Beto" in r2.pendente["texto"] and opcoes(r2)[0] == "confirmar"

    r3 = agente.decidir("c1", r2.pendente["id"], "confirmar")
    assert "Beto" in r3.mensagens[0] and status_alocacoes(db) == {"Beto": "confirmada"}


def test_alocar_tarefa_confirma_exatamente_quem_foi_mostrado(db, llm, agente):
    novo_func(db, "Ana", esp=(("Elétrica", 5),), anos_experiencia=10)
    t = nova_tarefa(db, [TEC])
    llm.responde("AlocarTarefa", tarefa_id=t.id)
    r = agente.enviar("c1", "completa a tarefa")
    assert "Ana" in r.pendente["texto"]

    # alguém melhor aparece depois da prévia: continua valendo quem o gestor aprovou
    novo_func(db, "Zeca", esp=(("Elétrica", 5),), anos_experiencia=30)
    agente.decidir("c1", r.pendente["id"], "confirmar")
    assert status_alocacoes(db) == {"Ana": "confirmada"}


# ---------- 1.2 e 1.7: contexto do modelo ----------


def test_contexto_mostra_certificacao_vencida(db):
    novo_func(db, "Helena", cert=(("NR10", date(2026, 10, 1)),))
    novo_func(db, "Ana", cert=(("NR35", date(2027, 1, 1)), ("CREA", None)))
    ctx = montar_contexto(db, AGORA)
    assert "NR10 (VENCIDA em 01/10/2026)" in ctx
    assert "NR35 (até 01/01/2027)" in ctx and "CREA" in ctx


def test_contexto_corta_as_tarefas_mais_distantes(db):
    for i in range(45):
        nova_tarefa(db, [TEC], titulo=f"Longe {i}", inicio=em(30 + i, 8), fim=em(30 + i, 17))
    nova_tarefa(db, [TEC], titulo="Amanhã cedo", inicio=em(1, 8), fim=em(1, 17))
    ctx = montar_contexto(db, AGORA)
    assert "Amanhã cedo" in ctx and "Longe 44" not in ctx and "não listadas" in ctx


# ---------- 3.2: intenções novas ----------


def test_criar_tarefa_aceita_prioridade_horas_e_experiencia(db, llm, agente):
    novo_func(db, "Junior", esp=(("Elétrica", 5),), anos_experiencia=1)
    novo_func(db, "Senior", esp=(("Elétrica", 4),), anos_experiencia=8)
    llm.responde("CriarTarefa", titulo="Urgente", data_inicio="2026-11-17", prioridade=5, horas_por_pessoa=4,
                 equipe=[{"papel": "tecnico", "anos_experiencia_min": 5}])
    r = agente.enviar("c1", "urgente: técnico sênior terça, 4h")
    assert "Senior" in r.pendente["texto"] and "prioridade 5" in r.pendente["texto"]
    agente.decidir("c1", r.pendente["id"], "criar_confirmar")
    [t] = tarefas(db)
    assert (t.prioridade, t.horas_por_pessoa, t.requisitos[0].anos_experiencia_min) == (5, 4, 5)


def test_substituir_na_tarefa(db, llm, agente):
    ana, beto = novo_func(db, "Ana"), novo_func(db, "Beto")
    sem_nr10 = novo_func(db, "Caio", cert=())
    t = nova_tarefa(db, [{**TEC, "certificacoes": ["NR10"]}])
    sv.confirmar(db, t, [schemas.Escolha(requisito_id=t.requisitos[0].id, funcionario_id=ana.id)])

    llm.responde("SubstituirNaTarefa", tarefa_id=t.id, sai_id=ana.id, entra_id=sem_nr10.id)
    r = agente.enviar("c1", "coloca o Caio no lugar da Ana")
    assert opcoes(r) == ["forcar", "descartar"] and "NR10" in r.pendente["texto"]
    assert "descartado" in agente.decidir("c1", r.pendente["id"], "descartar").mensagens[0]
    assert status_alocacoes(db) == {"Ana": "confirmada"}

    llm.responde("SubstituirNaTarefa", tarefa_id=t.id, sai_id=ana.id, entra_id=beto.id)
    r = agente.enviar("c1", "coloca o Beto no lugar da Ana")
    assert opcoes(r) == ["trocar", "descartar"]
    r2 = agente.decidir("c1", r.pendente["id"], "trocar")
    assert "Beto entrou no lugar de Ana" in r2.mensagens[0]
    assert status_alocacoes(db) == {"Ana": "cancelada", "Beto": "confirmada"}


def test_encerrar_tarefa(db, llm, agente):
    t = nova_tarefa(db, [TEC])
    llm.responde("EncerrarTarefa", tarefa_id=t.id, status="concluida")
    r = agente.enviar("c1", "conclui a tarefa")
    assert "concluída" in r.pendente["texto"]
    agente.decidir("c1", r.pendente["id"], "encerrar")
    assert tarefas(db)[0].status == "concluida"


def test_remarcar_mantem_horario_e_sugere_substituto(db, llm, agente):
    ana = novo_func(db, "Ana", esp=(("Elétrica", 5),))
    novo_func(db, "Beto")
    t = nova_tarefa(db, [TEC], inicio=em(1, 9), fim=em(1, 15))
    sv.confirmar(db, t, [schemas.Escolha(requisito_id=t.requisitos[0].id, funcionario_id=ana.id)])
    sv.criar_indisponibilidade(db, ana, schemas.IndisponibilidadeIn(inicio=em(3, 0), fim=em(4, 0), motivo="folga"))

    llm.responde("RemarcarTarefa", tarefa_id=t.id, data_inicio="2026-11-19")
    r = agente.enviar("c1", "passa a tarefa pra quinta")
    assert "qui 19/11, 09:00–15:00" in r.pendente["texto"] and "Ana" in r.pendente["texto"]
    r2 = agente.decidir("c1", r.pendente["id"], "remarcar")
    assert "remarcada" in r2.mensagens[0] and r2.pendente and "Beto" in r2.pendente["texto"]
    assert (tarefas(db)[0].inicio, tarefas(db)[0].fim) == (em(3, 9), em(3, 15))


def test_substituto_tem_ate_tres_botoes(db, llm, agente):
    ana = novo_func(db, "Ana", esp=(("Elétrica", 5),), anos_experiencia=10)
    for nome, nivel in (("Beto", 4), ("Caio", 4), ("Duda", 3), ("Edu", 3)):
        novo_func(db, nome, esp=(("Elétrica", nivel),))
    t = nova_tarefa(db, [TEC])
    sv.confirmar(db, t, [schemas.Escolha(requisito_id=t.requisitos[0].id, funcionario_id=ana.id)])
    llm.responde("TirarDaTarefa", tarefa_id=t.id, funcionario_id=ana.id)
    r = agente.enviar("c1", "tira a Ana")
    trocas = [v for v in opcoes(r) if v.startswith("trocar:")]
    assert len(trocas) == 3 and opcoes(r)[-2:] == ["retirar", "manter"]
    escolha = trocas[2]  # o terceiro da lista, com um toque
    r2 = agente.decidir("c1", r.pendente["id"], escolha)
    assert "entrou no lugar de Ana" in r2.mensagens[0]


def test_valor_de_botao_desconhecido_nao_troca_ninguem(db, llm, agente):
    ana = novo_func(db, "Ana")
    intruso = novo_func(db, "Intruso")
    t = nova_tarefa(db, [TEC])
    sv.confirmar(db, t, [schemas.Escolha(requisito_id=t.requisitos[0].id, funcionario_id=ana.id)])
    llm.responde("TirarDaTarefa", tarefa_id=t.id, funcionario_id=ana.id)
    r = agente.enviar("c1", "tira a Ana")
    agente.decidir("c1", r.pendente["id"], f"trocar:{intruso.id + 999}")
    assert status_alocacoes(db) == {"Ana": "confirmada"}


def test_consultas_de_vencimentos_e_contrato(db, llm, agente):
    novo_func(db, "Ana", cert=(("NR35", date(2026, 11, 20)),))
    sv.salvar_contrato(db, schemas.ContratoIn(
        codigo="CT-1", certificacoes=["NR10"],
        texto="Cláusula 1. Objeto.\n\nCláusula 2. Atraso gera multa de 2% ao mês."))
    llm.responde("ConsultarVencimentos", dias=30)
    assert "Ana · NR35 vence em 20/11/2026" in agente.enviar("c1", "quais NRs vencem?").mensagens[0]
    llm.responde("ConsultarContrato", codigo="ct-1", pergunta="multa por atraso")
    texto = agente.enviar("c1", "qual a multa do CT-1?").mensagens[0]
    assert "NR10" in texto and "multa de 2%" in texto
    llm.responde("ConsultarContrato", codigo="CT-99", pergunta="x")
    assert "Não conheço o contrato" in agente.enviar("c1", "e o CT-99?").mensagens[0]


# ---------- voz ----------


def test_mensagem_de_voz_vai_para_o_modelo_sem_ficar_no_historico(db, llm, agente):
    llm.responde("Responder", texto="Entendi o áudio")
    r = agente.enviar("c1", "🎤 (mensagem de voz)", audio={"base64": "T2dnUw==", "mime_type": "audio/ogg"})
    assert r.mensagens == ["Entendi o áudio"]
    ultima = llm.recebidas[0][-1]
    assert isinstance(ultima, HumanMessage) and ultima.content[1] == {
        "type": "audio", "base64": "T2dnUw==", "mime_type": "audio/ogg"}
    estado = agente.grafo.get_state(agente._cfg("c1")).values
    assert estado["audio"] is None and estado["messages"][0].content == "🎤 (mensagem de voz)"


# ---------- imprevisto avisado pela equipe ----------


def test_iniciar_substituicao_direto(db, llm, agente):
    ana = novo_func(db, "Ana", esp=(("Elétrica", 5),))
    beto = novo_func(db, "Beto")
    t = nova_tarefa(db, [TEC])
    [aloc] = sv.confirmar(db, t, [schemas.Escolha(requisito_id=t.requisitos[0].id, funcionario_id=ana.id)])
    r = agente.iniciar_substituicao("c1", [aloc.id], "Ana avisou um imprevisto.")
    assert r.mensagens[0] == "Ana avisou um imprevisto." and opcoes(r)[0] == f"trocar:{beto.id}"
    assert llm.recebidas == []  # não gastou chamada ao modelo
    agente.decidir("c1", r.pendente["id"], f"trocar:{beto.id}")
    # a conversa volta ao normal na mensagem seguinte
    llm.responde("Responder", texto="oi")
    assert agente.enviar("c1", "oi").mensagens == ["oi"]


# ---------- modo equipe ----------


def test_modo_equipe(db, llm):
    ana = novo_func(db, "Ana Souza")
    novo_func(db, "Beto Segredo")
    t = nova_tarefa(db, [TEC], titulo="Subestação", local="Betim", descricao="Levar termovisor")
    sv.confirmar(db, t, [schemas.Escolha(requisito_id=t.requisitos[0].id, funcionario_id=ana.id)])
    equipe = AssistenteEquipe(llm, agora=lambda: AGORA)

    llm.responde("MinhaAgenda", dias=7)
    assert "Subestação" in equipe.responder(ana.id, "minha agenda").mensagens[0]
    contexto = llm.recebidas[0][0].content
    assert "Ana Souza" in contexto and "Beto Segredo" not in contexto  # não vê o resto da equipe

    llm.responde("MinhaTarefa", data="2026-11-17")
    texto = equipe.responder(ana.id, "detalhes da tarefa de terça").mensagens[0]
    assert "Levar termovisor" in texto and "Betim" in texto

    llm.responde("MinhaTarefa", tarefa_id=t.id + 50)
    assert "não está escalado" in equipe.responder(ana.id, "e a tarefa 99?").mensagens[0]

    llm.responde("InformarAusencia", data_inicio="2026-11-17", motivo="atestado")
    resp = equipe.responder(ana.id, "estou de atestado terça")
    assert resp.solicitacao_id and "gestor" in resp.mensagens[0] and f"#{t.id}" in resp.mensagens[0]
    s = db.get(SolicitacaoAusencia, resp.solicitacao_id)
    assert (s.inicio, s.fim, s.status) == (em(1, 0), em(2, 0), "pendente")
    assert ana.indisponibilidades == []  # só vira ausência quando um gestor aprova


# ---------- bot: conversas por pessoa em grupo e botões com dono ----------


def test_conversa_em_grupo_e_por_pessoa():
    from app.bot import conversa_de, teclado_agente

    assert conversa_de(42, 42, privado=True) == "tg-42"
    assert conversa_de(-100123, 7, privado=False) != conversa_de(-100123, 8, privado=False)
    tec = teclado_agente({"id": "abcd1234", "opcoes": [["criar_confirmar", "Criar"], ["trocar:123456", "Trocar"]]},
                         1234567890123)
    dados = [b.callback_data for linha in tec.inline_keyboard for b in linha]
    assert dados[0] == "a|abcd1234|1234567890123|criar_confirmar"
    assert all(len(d.encode()) <= 64 for d in dados)  # limite do Telegram


# ---------- travas por conversa ----------


def test_mensagens_da_mesma_conversa_nao_rodam_juntas(db):
    ativos, pico = [0], [0]
    trava = threading.Lock()

    def modelo_lento(_mensagens):
        with trava:
            ativos[0] += 1
            pico[0] = max(pico[0], ativos[0])
        time.sleep(0.05)
        with trava:
            ativos[0] -= 1
        return "Responder", {"texto": "ok"}

    agente = Agente(construir_grafo(modelo_lento, InMemorySaver(), agora=lambda: AGORA))
    threads = [threading.Thread(target=agente.enviar, args=("mesma", f"m{i}")) for i in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert pico[0] == 1
    assert len(agente.grafo.get_state(agente._cfg("mesma")).values["messages"]) == 8
