"""Casos da revisão do projeto: horas por dia, escolha ótima, disponibilidade e regras novas do motor."""

from datetime import date

import pytest

from app import schemas
from app import services as sv
from app.allocation import disponiveis, horas_por_dia, propor
from app.parsing import especialidades

from .conftest import em, nova_tarefa, novo_func

TEC_ELETRICA = {"papel": "tecnico", "quantidade": 1, "especialidades": [{"nome": "Elétrica", "nivel_min": 3}]}


def nomes(avaliacoes):
    return [a.funcionario.nome for a in avaliacoes]


def motivos_de(vaga, nome):
    return next(a.motivos for a in vaga.rejeitados if a.funcionario.nome == nome)


def confirmar_tudo(db, t):
    sv.gerar_proposta(db, t)
    return sv.confirmar(db, t)


# ---------- horas por dia (1.3) ----------


def test_parada_de_fim_de_semana_nao_joga_tudo_na_sexta():
    # sexta 18h a domingo 17h, 20h por pessoa: antes iam 20h para a sexta e todo mundo era rejeitado
    dias = horas_por_dia(em(4, 18), em(6, 17), horas_por_pessoa=20)
    assert sorted(d.weekday() for d in dias) == [4, 5, 6]
    assert max(dias.values()) <= 8 and abs(sum(dias.values()) - 20) < 1e-9


def test_plantao_que_vira_a_madrugada_conta_a_duracao_real():
    assert horas_por_dia(em(1, 22), em(2, 2)) == {em(1, 0).date(): 2.0, em(2, 0).date(): 2.0}


def test_dias_parciais_contam_o_trecho_na_jornada():
    assert horas_por_dia(em(0, 15), em(1, 11)) == {em(0, 0).date(): 2.0, em(1, 0).date(): 3.0}


def test_quem_trabalhou_de_manha_pode_viajar_a_tarde(db):
    novo_func(db, "Ana")
    confirmar_tudo(db, nova_tarefa(db, [TEC_ELETRICA], inicio=em(0, 8), fim=em(0, 10)))
    viagem = nova_tarefa(db, [TEC_ELETRICA], inicio=em(0, 15), fim=em(1, 11))
    assert nomes(propor(db, viagem).vagas[0].escolhidos) == ["Ana"]


def test_parada_de_fim_de_semana_aceita_equipe(db):
    novo_func(db, "Ana")
    t = nova_tarefa(db, [TEC_ELETRICA], inicio=em(4, 18), fim=em(6, 17), horas_por_pessoa=20)
    assert nomes(propor(db, t).vagas[0].escolhidos) == ["Ana"]


# ---------- escolha da equipe (1.6) ----------


def test_escolha_otima_preenche_todas_as_vagas(db):
    # Folgas iguais (1 e 1). O guloso resolvia "2× Elétrica" primeiro, pegava X e Z (as maiores notas)
    # e deixava a vaga de SPDA, que só X e Z cumprem, vazia. Existe equipe completa: W entra em Elétrica.
    novo_func(db, "X", esp=(("Elétrica", 5), ("SPDA", 3)), anos_experiencia=10)
    novo_func(db, "Z", esp=(("Elétrica", 5), ("SPDA", 3)), anos_experiencia=10)
    novo_func(db, "W", esp=(("Elétrica", 2),))
    t = nova_tarefa(db, [
        {"papel": "tecnico", "quantidade": 2, "especialidades": [{"nome": "Elétrica", "nivel_min": 1}]},
        {"papel": "tecnico", "quantidade": 1, "especialidades": [{"nome": "SPDA", "nivel_min": 3}]},
    ])
    prop = propor(db, t)
    eletrica, spda = prop.vagas
    assert prop.completa
    assert "W" in nomes(eletrica.escolhidos) and len(spda.escolhidos) == 1


def test_escolha_otima_maximiza_a_pontuacao_somada(db):
    # A e B servem nas duas vagas. A é muito melhor em SPDA; em Elétrica os dois empatam.
    novo_func(db, "A", esp=(("Elétrica", 5), ("SPDA", 5)))
    novo_func(db, "B", esp=(("Elétrica", 5), ("SPDA", 1)))
    t = nova_tarefa(db, [
        {"papel": "tecnico", "quantidade": 1, "especialidades": [{"nome": "Elétrica", "nivel_min": 1}]},
        {"papel": "tecnico", "quantidade": 1, "especialidades": [{"nome": "SPDA", "nivel_min": 1}]},
    ])
    eletrica, spda = propor(db, t).vagas
    assert nomes(spda.escolhidos) == ["A"] and nomes(eletrica.escolhidos) == ["B"]


def test_candidato_de_outra_vaga_aparece_nos_rejeitados(db):
    novo_func(db, "Unico")
    t = nova_tarefa(db, [TEC_ELETRICA, {**TEC_ELETRICA, "desejaveis": ["SPDA"]}])
    v1, v2 = propor(db, t).vagas
    assert nomes(v1.escolhidos + v2.escolhidos) == ["Unico"]
    vazia = v2 if v1.escolhidos else v1
    assert nomes(vazia.rejeitados) == ["Unico"] and "outra vaga" in vazia.rejeitados[0].motivos[0]


# ---------- disponibilidade (1.5) ----------


def test_meio_periodo_aparece_livre_na_semana_e_no_dia(db):
    novo_func(db, "Igor", carga_horaria_semanal=30)
    novo_func(db, "Seis", horas_max_dia=6)
    assert nomes(disponiveis(db, em(0, 0), em(7, 0))) == ["Igor", "Seis"]
    assert nomes(disponiveis(db, em(0, 8), em(0, 17))) == ["Igor", "Seis"]


def test_disponiveis_tira_quem_ja_completou_a_jornada(db):
    novo_func(db, "Seis", horas_max_dia=6)
    confirmar_tudo(db, nova_tarefa(db, [TEC_ELETRICA], inicio=em(0, 8), fim=em(0, 14)))
    assert disponiveis(db, em(0, 15), em(0, 17)) == []


# ---------- regras novas do motor (3.1) ----------


def test_mesma_cidade_ganha_pontos_em_campo(db):
    novo_func(db, "Longe", base="Belo Horizonte")
    novo_func(db, "Perto", base="Betim")
    vaga = propor(db, nova_tarefa(db, [TEC_ELETRICA], tipo="campo", local="Betim/MG")).vagas[0]
    assert nomes(vaga.escolhidos) == ["Perto"] and "base em Betim" in vaga.escolhidos[0].detalhes["resumo"]


def test_tarefa_urgente_aponta_remanejamento(db):
    ana = novo_func(db, "Ana")
    baixa = nova_tarefa(db, [TEC_ELETRICA], titulo="Arquivo", prioridade=1)
    sv.confirmar(db, baixa, [schemas.Escolha(requisito_id=baixa.requisitos[0].id, funcionario_id=ana.id)])

    [rej] = propor(db, nova_tarefa(db, [TEC_ELETRICA], prioridade=5)).vagas[0].rejeitados
    assert rej.remanejar_de == [baixa.id] and rej.to_dict()["remanejar_de"] == [baixa.id]
    assert propor(db, nova_tarefa(db, [TEC_ELETRICA], prioridade=3)).vagas[0].rejeitados[0].remanejar_de == []


def test_descanso_minimo_entre_jornadas(db):
    novo_func(db, "Ana")
    confirmar_tudo(db, nova_tarefa(db, [TEC_ELETRICA], inicio=em(0, 18), fim=em(0, 23)))
    cedo = nova_tarefa(db, [TEC_ELETRICA], inicio=em(1, 8), fim=em(1, 12))  # 9h depois
    assert any("descanso entre jornadas" in m for m in motivos_de(propor(db, cedo).vagas[0], "Ana"))
    tarde = nova_tarefa(db, [TEC_ELETRICA], inicio=em(1, 13), fim=em(1, 17))  # 14h depois
    assert nomes(propor(db, tarde).vagas[0].escolhidos) == ["Ana"]


def test_tempo_de_deslocamento_entre_cidades(db):
    novo_func(db, "Ana")
    confirmar_tudo(db, nova_tarefa(db, [TEC_ELETRICA], tipo="campo", local="Contagem", inicio=em(0, 8), fim=em(0, 11)))
    outra = nova_tarefa(db, [TEC_ELETRICA], tipo="campo", local="Betim", inicio=em(0, 11, 30), fim=em(0, 14))
    assert any("deslocamento" in m for m in motivos_de(propor(db, outra).vagas[0], "Ana"))
    mesma = nova_tarefa(db, [TEC_ELETRICA], tipo="campo", local="Contagem/MG", inicio=em(0, 11, 30), fim=em(0, 14))
    assert nomes(propor(db, mesma).vagas[0].escolhidos) == ["Ana"]


def test_regras_do_contrato_valem_para_todas_as_vagas(db):
    so_nr10 = novo_func(db, "SoNR10")
    novo_func(db, "Completo", cert=(("NR10", None), ("NR35", None)), anos_experiencia=5)
    sv.salvar_contrato(db, schemas.ContratoIn(codigo="CT-1", certificacoes=["NR35"], anos_experiencia_min=3))
    t = nova_tarefa(db, [TEC_ELETRICA], contrato="ct-1")
    vaga = propor(db, t).vagas[0]
    assert nomes(vaga.escolhidos) == ["Completo"]
    motivos = motivos_de(vaga, "SoNR10")
    assert any("NR35" in m and "contrato CT-1" in m for m in motivos) and any("experiência" in m for m in motivos)
    with pytest.raises(sv.ErroNegocio):  # a confirmação manual também confere o contrato
        sv.confirmar(db, t, [schemas.Escolha(requisito_id=t.requisitos[0].id, funcionario_id=so_nr10.id)])


def test_contrato_que_exige_tecnico_de_seguranca_avisa(db):
    from app.allocation import contrato_por_codigo

    sv.salvar_contrato(db, schemas.ContratoIn(codigo="CT-2", exige_tecnico_seguranca=True))
    t = nova_tarefa(db, [TEC_ELETRICA], contrato="CT-2")
    assert sv.avisos_contrato(t, contrato_por_codigo(db, "CT-2"))
    t2 = nova_tarefa(db, [TEC_ELETRICA, {"papel": "Técnico de Segurança"}], contrato="CT-2")
    assert sv.avisos_contrato(t2, contrato_por_codigo(db, "CT-2")) == []


# ---------- cadastro (1.7) ----------


def test_certificacao_e_especialidade_repetidas(db):
    f = novo_func(db, "Renovou", cert=(("NR10", date(2026, 1, 1)), ("nr10", date(2027, 1, 1))),
                  esp=(("Elétrica", 2), ("eletrica", 4)))
    assert [(c.nome, c.validade) for c in f.certificacoes] == [("NR10", date(2027, 1, 1))]
    assert [(e.nome, e.nivel) for e in f.especialidades] == [("Elétrica", 4)]
    t = nova_tarefa(db, [{**TEC_ELETRICA, "certificacoes": ["NR10"]}])
    assert nomes(propor(db, t).vagas[0].escolhidos) == ["Renovou"]


def test_nivel_fora_de_1_a_5_e_erro():
    for texto in ("Elétrica:0", "Elétrica:99", "Elétrica:x"):
        with pytest.raises(ValueError):
            especialidades(texto, "nivel", 3)
    assert especialidades("Elétrica:5, SPDA", "nivel", 3) == [{"nome": "Elétrica", "nivel": 5}, {"nome": "SPDA", "nivel": 3}]


def test_telegram_id_repetido_e_recusado(db):
    novo_func(db, "Ana", telegram_id="123")
    with pytest.raises(sv.ErroNegocio):
        novo_func(db, "Beto", telegram_id="123")
