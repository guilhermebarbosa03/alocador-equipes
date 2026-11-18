"""Serviços novos: edição/remarcação, avisos para a equipe, pedidos de ausência, vencimentos, semana, contratos."""

from datetime import date, datetime

import pytest
from sqlalchemy import select

from app import contratos, schemas
from app import services as sv
from app.models import Auditoria, Notificacao

from .conftest import em, nova_tarefa, novo_func

TEC = {"papel": "tecnico", "quantidade": 1}


def avisos(db):
    db.expire_all()
    return [(n.funcionario.nome, n.tipo) for n in db.scalars(select(Notificacao).order_by(Notificacao.id))]


def dados_tarefa(t, **kw):
    base = {"titulo": t.titulo, "inicio": t.inicio, "fim": t.fim, "tipo": t.tipo, "local": t.local,
            "prioridade": t.prioridade, "contrato": t.contrato,
            "requisitos": [{"id": r.id, "papel": r.papel, "quantidade": r.quantidade} for r in t.requisitos]}
    return schemas.TarefaIn(**{**base, **kw})


def test_remarcar_revalida_e_nao_tira_ninguem_sozinho(db):
    ana = novo_func(db, "Ana", telegram_id="111")
    t = nova_tarefa(db, [TEC])
    sv.gerar_proposta(db, t)
    sv.confirmar(db, t)
    sv.criar_indisponibilidade(db, ana, schemas.IndisponibilidadeIn(inicio=em(3, 0), fim=em(4, 0), motivo="folga"))

    assert [a.funcionario_nome for a, _ in sv.simular_remarcacao(db, t, em(3, 8), em(3, 17))] == ["Ana"]
    problemas = sv.remarcar_tarefa(db, t, em(3, 8), em(3, 17), "teste")
    assert [(a.funcionario_nome, m[0][:11]) for a, m in problemas] == [("Ana", "indisponíve")]
    assert t.status == "alocada"  # continua confirmada; o gestor decide
    assert avisos(db) == [("Ana", "alocado"), ("Ana", "remarcado")]


def test_editar_tarefa_descarta_propostas_e_protege_vaga_confirmada(db):
    ana = novo_func(db, "Ana")
    novo_func(db, "Beto")
    t = nova_tarefa(db, [{**TEC, "quantidade": 2}])
    rid = t.requisitos[0].id
    sv.confirmar(db, t, [schemas.Escolha(requisito_id=rid, funcionario_id=ana.id)])
    sv.gerar_proposta(db, t)
    assert any(a.status == "proposta" for a in t.alocacoes)

    with pytest.raises(sv.ErroNegocio, match="remover a vaga"):  # a vaga da Ana sumiria
        sv.editar_tarefa(db, t, dados_tarefa(t, requisitos=[{"papel": "tecnico"}]))
    db.rollback()
    t = sv.obter_tarefa(db, t.id)
    with pytest.raises(sv.ErroNegocio, match="tem gente confirmada"):  # trocar a função de vaga ocupada
        sv.editar_tarefa(db, t, dados_tarefa(t, requisitos=[{"id": rid, "papel": "engenheiro", "quantidade": 2}]))
    db.rollback()
    t = sv.obter_tarefa(db, t.id)
    sv.editar_tarefa(db, t, dados_tarefa(t, titulo="Nova", requisitos=[
        {"id": rid, "papel": "tecnico", "quantidade": 2}, {"papel": "engenheiro", "quantidade": 1}]))
    assert t.titulo == "Nova" and [r.papel for r in t.requisitos] == ["tecnico", "engenheiro"]
    assert not any(a.status == "proposta" for a in t.alocacoes) and t.status == "parcial"


def test_avisos_so_para_quem_tem_telegram(db):
    ana = novo_func(db, "Ana", telegram_id="111")
    novo_func(db, "Beto")
    t = nova_tarefa(db, [{**TEC, "quantidade": 2}])
    sv.gerar_proposta(db, t)
    sv.confirmar(db, t)
    assert avisos(db) == [("Ana", "alocado")]

    aloc = next(a for a in t.alocacoes if a.funcionario_id == ana.id)
    sv.cancelar_alocacao(db, aloc, "teste")
    assert avisos(db)[-1] == ("Ana", "removido")
    assert db.scalar(select(Auditoria).where(Auditoria.acao == "cancelar_alocacao")).autor == "teste"


def test_resposta_ao_aviso(db):
    novo_func(db, "Ana", telegram_id="111")
    t = nova_tarefa(db, [TEC])
    sv.gerar_proposta(db, t)
    [aloc] = sv.confirmar(db, t)
    [n] = sv.notificacoes_pendentes(db)
    with pytest.raises(sv.ErroNegocio):  # só a própria pessoa responde
        sv.responder_notificacao(db, n.id, "999", "imprevisto")
    n, ainda_vale = sv.responder_notificacao(db, n.id, "111", "imprevisto")
    assert n.resposta == "imprevisto" and ainda_vale.id == aloc.id
    with pytest.raises(sv.ErroNegocio):
        sv.responder_notificacao(db, n.id, "111", "ciente")


def test_avisos_velhos_ou_de_tarefa_passada_expiram(db, monkeypatch):
    novo_func(db, "Ana", telegram_id="111")
    t = nova_tarefa(db, [TEC])
    sv.gerar_proposta(db, t)
    sv.confirmar(db, t)
    monkeypatch.setattr(sv, "agora", lambda: datetime(2026, 11, 20))
    assert sv.notificacoes_pendentes(db) == []
    assert db.scalar(select(Notificacao)).situacao == "expirada"


def test_pedido_de_ausencia_aprovado_vira_indisponibilidade(db):
    ana = novo_func(db, "Ana")
    t = nova_tarefa(db, [TEC])
    sv.gerar_proposta(db, t)
    sv.confirmar(db, t)
    s = sv.criar_solicitacao(db, ana, em(1, 0), em(2, 0), "atestado")
    assert [x.id for x in sv.solicitacoes(db)] == [s.id]
    afetadas = sv.decidir_solicitacao(db, s, True, "gestor")
    assert [a.tarefa_id for a in afetadas] == [t.id] and s.status == "aprovada"
    assert len(ana.indisponibilidades) == 1
    with pytest.raises(sv.ErroNegocio):
        sv.decidir_solicitacao(db, s, False, "gestor")


def test_vencimentos_ignora_a_vencida_que_foi_renovada(db):
    novo_func(db, "Renovou", cert=(("NR10", date(2026, 10, 1)), ("NR10", date(2027, 10, 1))))
    novo_func(db, "Vencida", cert=(("NR10", date(2026, 10, 1)),))
    novo_func(db, "Vence", cert=(("NR35", date(2026, 12, 1)),))
    novo_func(db, "Inativo", cert=(("NR35", date(2026, 11, 20)),), ativo=False)
    itens = sv.vencimentos(db, 30)
    assert [(v["nome"], v["dias"]) for v in itens] == [("Vencida", -45), ("Vence", 16)]
    assert "venceu em 01/10/2026" in sv.texto_vencimentos(itens)


def test_grade_da_semana(db):
    novo_func(db, "Ana", carga_horaria_semanal=40)
    t = nova_tarefa(db, [TEC], inicio=em(0, 8), fim=em(1, 17), tipo="campo")
    sv.gerar_proposta(db, t)
    sv.confirmar(db, t)
    [linha] = sv.grade_semana(db, em(0, 0).date())["linhas"]
    assert linha["horas"][:3] == [8.0, 8.0, 0.0] and linha["total"] == 16 and linha["pct"] == 40
    assert linha["celulas"][0][0]["campo"] and linha["celulas"][2] == []


def test_resumo_semanal_uma_vez_por_semana(db, monkeypatch):
    monkeypatch.setattr(sv, "agora", lambda: datetime(2026, 11, 16, 7, 0))  # segunda, antes das 8h
    assert not sv.resumo_semanal_devido(db)
    monkeypatch.setattr(sv, "agora", lambda: datetime(2026, 11, 16, 9, 0))
    assert sv.resumo_semanal_devido(db)
    sv.marcar_resumo_semanal(db, "Resumo")
    monkeypatch.setattr(sv, "agora", lambda: datetime(2026, 11, 18, 9, 0))  # quarta da mesma semana
    assert not sv.resumo_semanal_devido(db)
    monkeypatch.setattr(sv, "agora", lambda: datetime(2026, 11, 23, 8, 30))  # segunda seguinte
    assert sv.resumo_semanal_devido(db)


def test_busca_de_clausulas():
    texto = (
        "CLÁUSULA 1 – OBJETO\nManutenção das subestações.\n\n"
        "CLÁUSULA 5 – SEGURANÇA\nÉ obrigatório o uso de EPI: capacete e luvas isolantes.\n\n"
        "CLÁUSULA 8 – PENALIDADES\nAtraso no atendimento gera multa de 2% do valor mensal."
    )
    assert len(contratos.clausulas(texto)) == 3
    [trecho] = contratos.buscar_clausulas(texto, "qual a multa por atraso?", k=1)
    assert "multa de 2%" in trecho
    assert "EPI" in contratos.buscar_clausulas(texto, "que EPI o contrato exige?", k=1)[0]
    assert contratos.buscar_clausulas(texto, "o que é isso?") == []


def test_extrair_regras_com_modelo_falso():
    class Falso:
        def invoke(self, prompt):
            assert "NR35" in prompt
            return {"certificacoes": ["NR35"], "anos_experiencia_min": 2, "exige_tecnico_seguranca": True}

    regras = contratos.extrair_regras("A equipe deve ter NR35.", modelo=Falso())
    assert regras.certificacoes == ["NR35"] and regras.exige_tecnico_seguranca
