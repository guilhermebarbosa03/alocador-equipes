from datetime import date

import pytest

from app import schemas
from app import services as sv
from app.allocation import horas_por_dia, propor

from .conftest import em, nova_tarefa, novo_func

TEC_ELETRICA = {"papel": "tecnico", "quantidade": 1, "especialidades": [{"nome": "Elétrica", "nivel_min": 3}]}


def nomes(avaliacoes):
    return [a.funcionario.nome for a in avaliacoes]


def motivos_de(vaga, nome):
    return next(a.motivos for a in vaga.rejeitados if a.funcionario.nome == nome)


def test_escolhe_os_melhores_e_explica_rejeicao(db):
    novo_func(db, "Alto", esp=(("Elétrica", 5),), anos_experiencia=10)
    novo_func(db, "Medio", esp=(("eletrica", 4),), anos_experiencia=5)  # sem acento também casa
    novo_func(db, "Baixo", esp=(("Elétrica", 2),))
    t = nova_tarefa(db, [{**TEC_ELETRICA, "quantidade": 2}])

    vaga = propor(db, t).vagas[0]
    assert nomes(vaga.escolhidos) == ["Alto", "Medio"]
    assert vaga.faltam == 0
    assert any("nível 2" in m for m in motivos_de(vaga, "Baixo"))


def test_papel_diferente_nao_concorre(db):
    novo_func(db, "Eng", papel="engenheiro")
    t = nova_tarefa(db, [TEC_ELETRICA])
    vaga = propor(db, t).vagas[0]
    assert vaga.escolhidos == [] and vaga.rejeitados == [] and vaga.faltam == 1


def test_certificacao_vencida_antes_do_fim(db):
    novo_func(db, "Vencido", cert=(("NR10", date(2026, 11, 16)),))
    novo_func(db, "Ok", cert=(("NR10", date(2027, 1, 1)),))
    t = nova_tarefa(db, [{**TEC_ELETRICA, "certificacoes": ["nr10"]}], inicio=em(1, 8), fim=em(1, 17))
    vaga = propor(db, t).vagas[0]
    assert nomes(vaga.escolhidos) == ["Ok"]
    assert any("vence" in m for m in motivos_de(vaga, "Vencido"))


def test_campo_exige_disponibilidade_para_viagem(db):
    novo_func(db, "Fixo", disponivel_viagem=False)
    t = nova_tarefa(db, [TEC_ELETRICA], tipo="campo")
    vaga = propor(db, t).vagas[0]
    assert vaga.escolhidos == []
    assert any("campo" in m for m in motivos_de(vaga, "Fixo"))


def test_conflito_de_horario_com_tarefa_confirmada(db):
    novo_func(db, "Unico")
    t1 = nova_tarefa(db, [TEC_ELETRICA], inicio=em(1, 8), fim=em(1, 12))
    sv.gerar_proposta(db, t1)
    sv.confirmar(db, t1)

    t2 = nova_tarefa(db, [TEC_ELETRICA], inicio=em(1, 10), fim=em(1, 14))
    vaga = propor(db, t2).vagas[0]
    assert vaga.escolhidos == []
    assert any("já alocado" in m for m in motivos_de(vaga, "Unico"))

    t3 = nova_tarefa(db, [TEC_ELETRICA], inicio=em(1, 13), fim=em(1, 17))  # tarde livre
    assert nomes(propor(db, t3).vagas[0].escolhidos) == ["Unico"]


def test_carga_semanal(db):
    novo_func(db, "Meio", carga_horaria_semanal=30)
    t1 = nova_tarefa(db, [TEC_ELETRICA], inicio=em(0, 8), fim=em(2, 17))  # seg-qua = 24h
    sv.gerar_proposta(db, t1)
    sv.confirmar(db, t1)

    t2 = nova_tarefa(db, [TEC_ELETRICA], inicio=em(3, 8), fim=em(3, 17))  # +8h = 32h > 30h
    vaga = propor(db, t2).vagas[0]
    assert any("carga semanal" in m for m in motivos_de(vaga, "Meio"))

    t3 = nova_tarefa(db, [TEC_ELETRICA], inicio=em(3, 8), fim=em(3, 12))  # +4h = 28h
    assert nomes(propor(db, t3).vagas[0].escolhidos) == ["Meio"]


def test_vaga_mais_dificil_e_preenchida_primeiro(db):
    novo_func(db, "Completo", esp=(("Elétrica", 5), ("SPDA", 4)), anos_experiencia=10)
    novo_func(db, "SoEletrica", esp=(("Elétrica", 3),))
    t = nova_tarefa(db, [
        TEC_ELETRICA,
        {"papel": "tecnico", "quantidade": 1, "especialidades": [{"nome": "SPDA", "nivel_min": 3}]},
    ])
    eletrica, spda = propor(db, t).vagas
    # "Completo" seria o melhor nas duas, mas só ele serve para SPDA
    assert nomes(spda.escolhidos) == ["Completo"]
    assert nomes(eletrica.escolhidos) == ["SoEletrica"]


def test_rejeitados_com_perfil_aparecem_primeiro(db):
    novo_func(db, "Aaa SemPerfil", esp=(("Elétrica", 1),))  # nome vem antes na ordem alfabética
    ocupado = novo_func(db, "Zzz Ocupado", esp=(("Elétrica", 5),))
    t1 = nova_tarefa(db, [TEC_ELETRICA])
    sv.confirmar(db, t1, [schemas.Escolha(requisito_id=t1.requisitos[0].id, funcionario_id=ocupado.id)])

    vaga = propor(db, nova_tarefa(db, [TEC_ELETRICA])).vagas[0]
    assert nomes(vaga.rejeitados) == ["Zzz Ocupado", "Aaa SemPerfil"]
    assert [a.perfil_ok for a in vaga.rejeitados] == [True, False]


def test_informa_quantos_faltam(db):
    novo_func(db, "Um")
    prop = propor(db, nova_tarefa(db, [{**TEC_ELETRICA, "quantidade": 3}]))
    assert prop.vagas[0].faltam == 2 and not prop.completa


def test_ausencia_aponta_alocacoes_afetadas_e_confirmar_revalida(db):
    f = novo_func(db, "Ana")
    t1 = nova_tarefa(db, [TEC_ELETRICA])
    sv.gerar_proposta(db, t1)
    sv.confirmar(db, t1)

    _, afetadas = sv.criar_indisponibilidade(
        db, f, schemas.IndisponibilidadeIn(inicio=em(1, 0), fim=em(2, 0), motivo="atestado")
    )
    assert [a.tarefa_id for a in afetadas] == [t1.id]

    # proposta gerada antes da ausência não pode ser confirmada sem forçar
    t2 = nova_tarefa(db, [TEC_ELETRICA], inicio=em(3, 8), fim=em(3, 17))
    sv.gerar_proposta(db, t2)
    sv.criar_indisponibilidade(db, f, schemas.IndisponibilidadeIn(inicio=em(3, 0), fim=em(4, 0), motivo="folga"))
    with pytest.raises(sv.ErroNegocio) as e:
        sv.confirmar(db, t2)
    assert e.value.status == 409 and "Ana" in e.value.problemas

    [aloc] = sv.confirmar(db, t2, forcar=True)
    assert aloc.status == "confirmada" and aloc.justificativa["forcado"] is True


def test_nova_proposta_mantem_quem_ja_foi_confirmado(db):
    a = novo_func(db, "A", esp=(("Elétrica", 5),))
    novo_func(db, "B", esp=(("Elétrica", 4),))
    t = nova_tarefa(db, [{**TEC_ELETRICA, "quantidade": 2}])
    req_id = t.requisitos[0].id
    sv.confirmar(db, t, [schemas.Escolha(requisito_id=req_id, funcionario_id=a.id)])
    assert t.status == "parcial"

    prop = sv.gerar_proposta(db, t)
    assert prop.vagas[0].ja_confirmados == [a.id]
    assert nomes(prop.vagas[0].escolhidos) == ["B"]
    sv.confirmar(db, t)
    assert t.status == "alocada"


def test_confirmar_nao_passa_da_quantidade(db):
    a, b = novo_func(db, "A"), novo_func(db, "B")
    t = nova_tarefa(db, [TEC_ELETRICA])
    rid = t.requisitos[0].id
    with pytest.raises(sv.ErroNegocio):
        sv.confirmar(db, t, [schemas.Escolha(requisito_id=rid, funcionario_id=a.id),
                             schemas.Escolha(requisito_id=rid, funcionario_id=b.id)])


def test_horas_por_dia():
    assert horas_por_dia(em(0, 8), em(0, 17)) == {em(0, 0).date(): 8.0}  # desconta almoço
    assert horas_por_dia(em(0, 8), em(0, 12)) == {em(0, 0).date(): 4.0}
    # sexta a segunda: só dias úteis contam
    dias = horas_por_dia(em(4, 8), em(7, 17))
    assert sorted(d.weekday() for d in dias) == [0, 4] and set(dias.values()) == {8.0}
    assert sum(horas_por_dia(em(0, 8), em(1, 17), horas_por_pessoa=10).values()) == 10
