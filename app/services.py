"""Regras de negócio usadas pela API, pelo painel e, na fase 2, pelas ferramentas do agente."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import schemas
from .allocation import Avaliacao, Janela, Proposta, Vaga, avaliar, carregar_compromissos, norm, propor
from .models import Alocacao, Certificacao, Especialidade, Funcionario, Indisponibilidade, Requisito, Tarefa

ENCERRADAS = ("cancelada", "concluida")


class ErroNegocio(Exception):
    def __init__(self, mensagem: str, status: int = 400, problemas: dict | None = None):
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.status = status
        self.problemas = problemas


# ---------- Funcionários ----------


def obter_funcionario(db: Session, funcionario_id: int) -> Funcionario:
    f = db.get(Funcionario, funcionario_id)
    if f is None:
        raise ErroNegocio(f"Funcionário #{funcionario_id} não encontrado", 404)
    return f


def listar_funcionarios(db: Session, incluir_inativos: bool = False, papel: str | None = None) -> list[Funcionario]:
    q = select(Funcionario).order_by(Funcionario.nome)
    if not incluir_inativos:
        q = q.where(Funcionario.ativo.is_(True))
    out = db.scalars(q).all()
    return [f for f in out if papel is None or norm(f.papel) == norm(papel)]


def _aplicar_dados_funcionario(f: Funcionario, dados: schemas.FuncionarioIn) -> None:
    for campo in (
        "nome", "papel", "email", "telegram_id", "carga_horaria_semanal", "horas_max_dia",
        "anos_experiencia", "disponivel_viagem", "base", "resumo",
    ):
        setattr(f, campo, getattr(dados, campo))
    f.papel = f.papel.strip()
    f.especialidades = [Especialidade(nome=e.nome.strip(), nivel=e.nivel) for e in dados.especialidades]
    f.certificacoes = [Certificacao(nome=c.nome.strip(), validade=c.validade) for c in dados.certificacoes]


def criar_funcionario(db: Session, dados: schemas.FuncionarioIn) -> Funcionario:
    f = Funcionario(ativo=True)
    _aplicar_dados_funcionario(f, dados)
    db.add(f)
    db.commit()
    return f


def atualizar_funcionario(db: Session, f: Funcionario, dados: schemas.FuncionarioIn) -> Funcionario:
    _aplicar_dados_funcionario(f, dados)
    db.commit()
    return f


def desativar_funcionario(db: Session, f: Funcionario) -> Funcionario:
    f.ativo = False
    db.commit()
    return f


def _alocacoes_no_intervalo(db: Session, funcionario_id: int, inicio: datetime, fim: datetime, status: tuple):
    return db.scalars(
        select(Alocacao)
        .join(Tarefa, Alocacao.tarefa_id == Tarefa.id)
        .where(
            Alocacao.funcionario_id == funcionario_id,
            Alocacao.status.in_(status),
            Tarefa.status.not_in(ENCERRADAS),
            Tarefa.inicio < fim,
            Tarefa.fim > inicio,
        )
        .order_by(Tarefa.inicio)
    ).all()


def alocacoes_confirmadas(db: Session, funcionario_id: int, inicio: datetime, fim: datetime) -> list[Alocacao]:
    return list(_alocacoes_no_intervalo(db, funcionario_id, inicio, fim, ("confirmada",)))


def criar_indisponibilidade(
    db: Session, f: Funcionario, dados: schemas.IndisponibilidadeIn
) -> tuple[Indisponibilidade, list[Alocacao]]:
    """Grava a ausência e devolve as alocações confirmadas que ela atinge (não cancela nada sozinha)."""
    ind = Indisponibilidade(funcionario_id=f.id, inicio=dados.inicio, fim=dados.fim, motivo=dados.motivo)
    db.add(ind)
    db.commit()
    afetadas = _alocacoes_no_intervalo(db, f.id, dados.inicio, dados.fim, ("confirmada",))
    return ind, list(afetadas)


def remover_indisponibilidade(db: Session, indisponibilidade_id: int) -> None:
    ind = db.get(Indisponibilidade, indisponibilidade_id)
    if ind is None:
        raise ErroNegocio(f"Indisponibilidade #{indisponibilidade_id} não encontrada", 404)
    db.delete(ind)
    db.commit()


def agenda(db: Session, f: Funcionario, inicio: datetime, fim: datetime) -> list[dict]:
    eventos = [
        {
            "tipo": "tarefa", "id": a.tarefa.id, "titulo": a.tarefa.titulo,
            "inicio": a.tarefa.inicio, "fim": a.tarefa.fim, "status": a.status,
        }
        for a in _alocacoes_no_intervalo(db, f.id, inicio, fim, ("confirmada", "proposta"))
    ]
    indis = db.scalars(
        select(Indisponibilidade).where(
            Indisponibilidade.funcionario_id == f.id,
            Indisponibilidade.inicio < fim,
            Indisponibilidade.fim > inicio,
        )
    ).all()
    eventos += [
        {"tipo": "indisponibilidade", "id": i.id, "titulo": i.motivo, "inicio": i.inicio, "fim": i.fim}
        for i in indis
    ]
    return sorted(eventos, key=lambda e: e["inicio"])


# ---------- Tarefas ----------


def obter_tarefa(db: Session, tarefa_id: int) -> Tarefa:
    t = db.get(Tarefa, tarefa_id)
    if t is None:
        raise ErroNegocio(f"Tarefa #{tarefa_id} não encontrada", 404)
    return t


def listar_tarefas(db: Session, status: str | None = None) -> list[Tarefa]:
    q = select(Tarefa).order_by(Tarefa.inicio.desc())
    if status:
        q = q.where(Tarefa.status == status)
    return list(db.scalars(q).all())


def criar_tarefa(db: Session, dados: schemas.TarefaIn) -> Tarefa:
    t = _montar_tarefa(dados)
    db.add(t)
    db.commit()
    return t


def simular_proposta(db: Session, dados: schemas.TarefaIn) -> Proposta:
    """Roda o motor para uma tarefa que ainda não existe, sem gravar nada."""
    return propor(db, _montar_tarefa(dados))


def _montar_tarefa(dados: schemas.TarefaIn) -> Tarefa:
    t = Tarefa(
        titulo=dados.titulo, descricao=dados.descricao, tipo=dados.tipo, local=dados.local,
        inicio=dados.inicio, fim=dados.fim, horas_por_pessoa=dados.horas_por_pessoa,
        prioridade=dados.prioridade, contrato=dados.contrato, status="aberta",
    )
    t.requisitos = [
        Requisito(
            papel=r.papel.strip(),
            quantidade=r.quantidade,
            especialidades=[e.model_dump() for e in r.especialidades],
            desejaveis=[d.strip() for d in r.desejaveis if d.strip()],
            certificacoes=[c.strip() for c in r.certificacoes if c.strip()],
            anos_experiencia_min=r.anos_experiencia_min,
        )
        for r in dados.requisitos
    ]
    return t


def atualizar_status(t: Tarefa) -> None:
    if t.status in ENCERRADAS:
        return
    conf = Counter(a.requisito_id for a in t.alocacoes if a.status == "confirmada")
    if t.requisitos and all(conf[r.id] >= r.quantidade for r in t.requisitos):
        t.status = "alocada"
    elif sum(conf.values()):
        t.status = "parcial"
    elif any(a.status == "proposta" for a in t.alocacoes):
        t.status = "proposta"
    else:
        t.status = "aberta"


def _exigir_ativa(t: Tarefa) -> None:
    if t.status in ENCERRADAS:
        raise ErroNegocio(f"Tarefa #{t.id} está {t.status}", 409)


def gerar_proposta(db: Session, t: Tarefa) -> Proposta:
    """Roda o motor e grava os escolhidos como alocações 'proposta' (substitui a proposta anterior)."""
    _exigir_ativa(t)
    prop = propor(db, t)
    t.alocacoes = [a for a in t.alocacoes if a.status != "proposta"]
    for vaga in prop.vagas:
        for av in vaga.escolhidos:
            t.alocacoes.append(
                Alocacao(
                    requisito_id=vaga.requisito.id, funcionario_id=av.funcionario.id, funcionario=av.funcionario,
                    status="proposta", score=av.score, justificativa=av.detalhes,
                )
            )
    atualizar_status(t)
    db.commit()
    return prop


def confirmar(
    db: Session, t: Tarefa, escolhas: list[schemas.Escolha] | None = None, forcar: bool = False
) -> list[Alocacao]:
    """Confirma a proposta atual (ou escolhas manuais), revalidando as regras na hora."""
    _exigir_ativa(t)
    reqs = {r.id: r for r in t.requisitos}

    if escolhas is None:
        pares = [(a.requisito_id, a.funcionario_id) for a in t.alocacoes if a.status == "proposta"]
        if not pares:
            raise ErroNegocio("Não há proposta para confirmar. Gere uma proposta antes.", 409)
    else:
        pares = [(e.requisito_id, e.funcionario_id) for e in escolhas]
        if not pares:
            raise ErroNegocio("Nenhuma escolha informada", 400)

    ids = [fid for _, fid in pares]
    if len(set(ids)) != len(ids):
        raise ErroNegocio("A mesma pessoa aparece mais de uma vez", 400)
    ja_confirmados = {a.funcionario_id for a in t.alocacoes if a.status == "confirmada"}
    if repetidos := ja_confirmados & set(ids):
        raise ErroNegocio(f"Já confirmados nesta tarefa: {sorted(repetidos)}", 409)
    for rid, _ in pares:
        if rid not in reqs:
            raise ErroNegocio(f"Requisito #{rid} não pertence à tarefa #{t.id}", 400)

    conf = Counter(a.requisito_id for a in t.alocacoes if a.status == "confirmada")
    for rid, n in Counter(rid for rid, _ in pares).items():
        if conf[rid] + n > reqs[rid].quantidade:
            raise ErroNegocio(
                f"A vaga de '{reqs[rid].papel}' pede {reqs[rid].quantidade}; "
                f"já há {conf[rid]} confirmado(s) e foram escolhidos mais {n}",
                409,
            )

    funcs = {f.id: f for f in db.scalars(select(Funcionario).where(Funcionario.id.in_(ids))).all()}
    if faltando := set(ids) - set(funcs):
        raise ErroNegocio(f"Funcionário(s) não encontrado(s): {sorted(faltando)}", 404)

    comps = carregar_compromissos(db, Janela.de(t), ids)
    resultados, problemas = [], {}
    for rid, fid in pares:
        f, req = funcs[fid], reqs[rid]
        av = avaliar(f, req, Janela.de(t), comps[fid])
        motivos = list(av.motivos)
        if norm(f.papel) != norm(req.papel):
            motivos.insert(0, f"papel '{f.papel}' diferente do pedido '{req.papel}'")
        if motivos:
            problemas[f.nome] = motivos
        resultados.append((rid, f, av, motivos))

    if problemas and not forcar:
        raise ErroNegocio("A alocação viola regras. Use forcar=true para confirmar mesmo assim.", 409, problemas)

    if escolhas is None:
        t.alocacoes = [a for a in t.alocacoes if a.status != "proposta"]
    else:  # escolha manual: descarta só as propostas da(s) vaga(s) e pessoa(s) envolvidas
        vagas_afetadas, pessoas = {rid for rid, _ in pares}, set(ids)
        t.alocacoes = [
            a for a in t.alocacoes
            if not (a.status == "proposta" and (a.requisito_id in vagas_afetadas or a.funcionario_id in pessoas))
        ]
    novas = []
    for rid, f, av, motivos in resultados:
        just = dict(av.detalhes)
        if motivos:
            just.update(forcado=True, violacoes=motivos)
        a = Alocacao(
            requisito_id=rid, funcionario_id=f.id, funcionario=f,
            status="confirmada", score=av.score, justificativa=just,
        )
        t.alocacoes.append(a)
        novas.append(a)
    atualizar_status(t)
    db.commit()
    return novas


def obter_alocacao(db: Session, alocacao_id: int) -> Alocacao:
    a = db.get(Alocacao, alocacao_id)
    if a is None:
        raise ErroNegocio(f"Alocação #{alocacao_id} não encontrada", 404)
    return a


def cancelar_alocacao(db: Session, a: Alocacao) -> Alocacao:
    a.status = "cancelada"
    atualizar_status(a.tarefa)
    db.commit()
    return a


def sugerir_substituto(db: Session, a: Alocacao) -> tuple[Avaliacao | None, Vaga | None]:
    """Melhor pessoa para ocupar a vaga de `a`, como se `a` já tivesse sido cancelada."""
    prop = propor(db, a.tarefa, excluir_funcionarios={a.funcionario_id}, ignorar_alocacoes={a.id})
    vaga = next((v for v in prop.vagas if v.requisito.id == a.requisito_id), None)
    return (vaga.escolhidos[0] if vaga and vaga.escolhidos else None), vaga


def substituir(db: Session, a: Alocacao, novo_funcionario_id: int) -> Alocacao:
    """Cancela `a` e confirma outra pessoa na mesma vaga (regras checadas de novo)."""
    if a.status != "confirmada":
        raise ErroNegocio(f"A alocação #{a.id} não está confirmada", 409)
    a.status = "cancelada"
    try:
        [nova] = confirmar(db, a.tarefa, [schemas.Escolha(requisito_id=a.requisito_id, funcionario_id=novo_funcionario_id)])
    except ErroNegocio:
        db.rollback()
        raise
    return nova


def encerrar_tarefa(db: Session, t: Tarefa, status: str) -> Tarefa:
    assert status in ENCERRADAS
    _exigir_ativa(t)
    t.status = status
    if status == "cancelada":
        for a in t.alocacoes:
            if a.status in ("proposta", "confirmada"):
                a.status = "cancelada"
    else:
        t.alocacoes = [a for a in t.alocacoes if a.status != "proposta"]
    db.commit()
    return t
