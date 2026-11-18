"""Regras de negócio usadas pela API, pelo painel e pelas ferramentas do agente.

Funções que gravam recebem `autor` ("painel", "api", "telegram:Fulano"...), que vai para a auditoria.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, contains_eager, joinedload

from . import config, schemas
from .allocation import (
    Avaliacao, Janela, Proposta, Vaga, avaliar, carregar_compromissos, contrato_por_codigo, efetivo,
    horas_por_dia, norm, propor, sobrepoe,
)
from .models import (
    Alocacao, Auditoria, Certificacao, Contrato, Especialidade, Funcionario, Indisponibilidade, Notificacao,
    Requisito, SolicitacaoAusencia, Tarefa,
)
from .relogio import agora, hoje
from .texto import quando

ENCERRADAS = ("cancelada", "concluida")


class ErroNegocio(Exception):
    def __init__(self, mensagem: str, status: int = 400, problemas: dict | None = None):
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.status = status
        self.problemas = problemas


# ---------- Auditoria e avisos ----------


def auditar(
    db: Session, acao: str, resumo: str, autor: str = "sistema", tarefa_id: int | None = None,
    funcionario_id: int | None = None, detalhes: dict | None = None,
) -> None:
    """Registra na sessão; quem chamou faz o commit junto com a mudança."""
    db.add(Auditoria(quando=agora(), acao=acao, resumo=resumo[:400], autor=(autor or "sistema")[:120],
                     tarefa_id=tarefa_id, funcionario_id=funcionario_id, detalhes=detalhes))


def listar_auditoria(db: Session, acao: str | None = None, tarefa_id: int | None = None, limite: int = 200):
    q = select(Auditoria).order_by(Auditoria.quando.desc(), Auditoria.id.desc()).limit(limite)
    if acao:
        q = q.where(Auditoria.acao == acao)
    if tarefa_id:
        q = q.where(Auditoria.tarefa_id == tarefa_id)
    return list(db.scalars(q).all())


def _avisar(db: Session, f: Funcionario, tipo: str, t: Tarefa, texto: str, alocacao: Alocacao | None = None) -> None:
    """Põe um aviso na caixa de saída do bot (só para quem tem Telegram e para tarefas que não passaram)."""
    if not config.AVISOS_EQUIPE or not f.telegram_id or t.fim < agora():
        return
    db.add(Notificacao(funcionario_id=f.id, alocacao_id=alocacao.id if alocacao else None, tarefa_id=t.id,
                       tipo=tipo, texto=texto))


def _resumo_tarefa(t: Tarefa) -> str:
    local = f" · {t.local}" if t.local else ""
    contrato = f" · contrato {t.contrato}" if t.contrato else ""
    return f"#{t.id} {t.titulo}\n{quando(t.inicio, t.fim)} · {t.tipo}{local}{contrato}"


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


def _aplicar_dados_funcionario(db: Session, f: Funcionario, dados: schemas.FuncionarioIn) -> None:
    if dados.telegram_id:
        outro = db.scalar(select(Funcionario).where(
            Funcionario.telegram_id == dados.telegram_id, Funcionario.ativo.is_(True),
            Funcionario.id != (f.id or 0),
        ))
        if outro:
            raise ErroNegocio(f"O ID do Telegram {dados.telegram_id} já está no cadastro de {outro.nome}", 409)
    for campo in (
        "nome", "papel", "email", "telegram_id", "carga_horaria_semanal", "horas_max_dia",
        "anos_experiencia", "disponivel_viagem", "base", "resumo",
    ):
        setattr(f, campo, getattr(dados, campo))
    f.papel = f.papel.strip()
    f.especialidades = [Especialidade(nome=e.nome.strip(), nivel=e.nivel) for e in dados.especialidades]
    f.certificacoes = [Certificacao(nome=c.nome.strip(), validade=c.validade) for c in dados.certificacoes]


def criar_funcionario(db: Session, dados: schemas.FuncionarioIn) -> Funcionario:
    f = Funcionario(ativo=True if dados.ativo is None else dados.ativo)
    _aplicar_dados_funcionario(db, f, dados)
    db.add(f)
    db.commit()
    return f


def atualizar_funcionario(db: Session, f: Funcionario, dados: schemas.FuncionarioIn, autor: str = "sistema") -> Funcionario:
    _aplicar_dados_funcionario(db, f, dados)
    if dados.ativo is True and not f.ativo:
        return reativar_funcionario(db, f, autor)
    if dados.ativo is False and f.ativo:
        desativar_funcionario(db, f, forcar=True, autor=autor)
        return f
    db.commit()
    return f


def alocacoes_futuras(db: Session, f: Funcionario) -> list[Alocacao]:
    return list(_alocacoes_no_intervalo(db, f.id, agora(), datetime.max, ("confirmada",)))


def desativar_funcionario(db: Session, f: Funcionario, forcar: bool = False, autor: str = "sistema") -> list[Alocacao]:
    """Desativa (não apaga). Sem `forcar`, recusa se a pessoa ainda está confirmada em tarefas futuras.
    Devolve essas alocações: elas continuam valendo até alguém substituir a pessoa."""
    futuras = alocacoes_futuras(db, f)
    if futuras and not forcar:
        raise ErroNegocio(
            f"{f.nome} está confirmado(a) em {len(futuras)} tarefa(s) futura(s). "
            "Substitua antes ou use forcar=true para desativar mesmo assim.",
            409, {f.nome: [f"#{a.tarefa.id} {a.tarefa.titulo} ({quando(a.tarefa.inicio, a.tarefa.fim)})" for a in futuras]},
        )
    f.ativo = False
    auditar(db, "desativar", f"{f.nome} desativado(a)" + (f" com {len(futuras)} alocação(ões) futura(s)" if futuras else ""),
            autor, funcionario_id=f.id, detalhes={"tarefas": [a.tarefa_id for a in futuras]} if futuras else None)
    db.commit()
    return futuras


def reativar_funcionario(db: Session, f: Funcionario, autor: str = "sistema") -> Funcionario:
    if f.telegram_id and db.scalar(select(Funcionario.id).where(
        Funcionario.telegram_id == f.telegram_id, Funcionario.ativo.is_(True), Funcionario.id != f.id
    )):
        raise ErroNegocio(f"Outro funcionário ativo já usa o ID do Telegram {f.telegram_id}", 409)
    f.ativo = True
    auditar(db, "reativar", f"{f.nome} reativado(a)", autor, funcionario_id=f.id)
    db.commit()
    return f


def _alocacoes_no_intervalo(db: Session, funcionario_id: int, inicio: datetime, fim: datetime, status: tuple):
    return db.scalars(
        select(Alocacao)
        .join(Tarefa, Alocacao.tarefa_id == Tarefa.id)
        .options(contains_eager(Alocacao.tarefa))
        .where(
            Alocacao.funcionario_id == funcionario_id,
            Alocacao.status.in_(status),
            Tarefa.status.not_in(ENCERRADAS),
            Tarefa.inicio < fim,
            Tarefa.fim > inicio,
        )
        .order_by(Tarefa.inicio)
    ).unique().all()


def alocacoes_confirmadas(db: Session, funcionario_id: int, inicio: datetime, fim: datetime) -> list[Alocacao]:
    return list(_alocacoes_no_intervalo(db, funcionario_id, inicio, fim, ("confirmada",)))


def criar_indisponibilidade(
    db: Session, f: Funcionario, dados: schemas.IndisponibilidadeIn, autor: str = "sistema"
) -> tuple[Indisponibilidade, list[Alocacao]]:
    """Grava a ausência e devolve as alocações confirmadas que ela atinge (não cancela nada sozinha)."""
    ind = Indisponibilidade(funcionario_id=f.id, inicio=dados.inicio, fim=dados.fim, motivo=dados.motivo)
    db.add(ind)
    afetadas = list(_alocacoes_no_intervalo(db, f.id, dados.inicio, dados.fim, ("confirmada",)))
    auditar(db, "ausencia", f"Ausência de {f.nome} ({dados.motivo}): {quando(dados.inicio, dados.fim)}", autor,
            funcionario_id=f.id, detalhes={"tarefas_afetadas": [a.tarefa_id for a in afetadas]} if afetadas else None)
    db.commit()
    return ind, afetadas


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
            "local": a.tarefa.local, "tarefa_tipo": a.tarefa.tipo,
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


def vencimentos(db: Session, dias: int = 30) -> list[dict]:
    """Certificações vencidas ou que vencem nos próximos `dias`, dos funcionários ativos.
    Se a pessoa tem a mesma certificação repetida (vencida e renovada), vale a mais longa."""
    base, out = hoje(), []
    limite = base + timedelta(days=dias)
    for f in listar_funcionarios(db):
        melhores: dict[str, Certificacao] = {}
        for c in f.certificacoes:
            k, atual = norm(c.nome), melhores.get(norm(c.nome))
            if atual is None or (atual.validade is not None and (c.validade is None or c.validade > atual.validade)):
                melhores[k] = c
        for c in melhores.values():
            if c.validade is not None and c.validade <= limite:
                out.append({"funcionario_id": f.id, "nome": f.nome, "funcionario": f, "certificacao": c.nome,
                            "validade": c.validade, "dias": (c.validade - base).days})
    return sorted(out, key=lambda v: (v["validade"], v["nome"]))


def texto_vencimentos(itens: list[dict], dias: int = 30) -> str:
    if not itens:
        return f"Nenhuma certificação vencida ou vencendo nos próximos {dias} dias."
    linhas = []
    for v in itens:
        if v["dias"] < 0:
            situacao = f"venceu em {v['validade']:%d/%m/%Y}"
        elif v["dias"] == 0:
            situacao = "vence hoje"
        else:
            situacao = f"vence em {v['validade']:%d/%m/%Y} ({v['dias']} dia(s))"
        linhas.append(f"• {v['nome']} · {v['certificacao']} {situacao}")
    return f"Certificações vencidas ou vencendo em {dias} dias:\n" + "\n".join(linhas)


# ---------- Tarefas ----------


def obter_tarefa(db: Session, tarefa_id: int) -> Tarefa:
    t = db.get(Tarefa, tarefa_id)
    if t is None:
        raise ErroNegocio(f"Tarefa #{tarefa_id} não encontrada", 404)
    return t


def listar_tarefas(
    db: Session, status: str | None = None, de: date | None = None, ate: date | None = None,
    tipo: str | None = None, contrato: str | None = None, recentes_primeiro: bool = True,
) -> list[Tarefa]:
    q = select(Tarefa).order_by(Tarefa.inicio.desc() if recentes_primeiro else Tarefa.inicio, Tarefa.id)
    if status == "ativas":
        q = q.where(Tarefa.status.not_in(ENCERRADAS))
    elif status:
        q = q.where(Tarefa.status == status)
    if de:
        q = q.where(Tarefa.fim > datetime.combine(de, time.min))
    if ate:
        q = q.where(Tarefa.inicio < datetime.combine(ate + timedelta(days=1), time.min))
    if tipo:
        q = q.where(Tarefa.tipo == tipo)
    out = list(db.scalars(q).all())
    if contrato:
        out = [t for t in out if t.contrato and norm(contrato) in norm(t.contrato)]
    return out


def criar_tarefa(db: Session, dados: schemas.TarefaIn) -> Tarefa:
    t = _montar_tarefa(dados)
    db.add(t)
    db.commit()
    return t


def simular_proposta(db: Session, dados: schemas.TarefaIn) -> Proposta:
    """Roda o motor para uma tarefa que ainda não existe, sem gravar nada."""
    return propor(db, _montar_tarefa(dados))


def _aplicar_requisito(req: Requisito, r: schemas.RequisitoIn) -> Requisito:
    req.papel = r.papel.strip()
    req.quantidade = r.quantidade
    req.especialidades = [e.model_dump() for e in r.especialidades]
    req.desejaveis = list(r.desejaveis)
    req.certificacoes = list(r.certificacoes)
    req.anos_experiencia_min = r.anos_experiencia_min
    return req


def _montar_tarefa(dados: schemas.TarefaIn) -> Tarefa:
    t = Tarefa(
        titulo=dados.titulo, descricao=dados.descricao, tipo=dados.tipo, local=dados.local,
        inicio=dados.inicio, fim=dados.fim, horas_por_pessoa=dados.horas_por_pessoa,
        prioridade=dados.prioridade, contrato=(dados.contrato or "").strip() or None, status="aberta",
    )
    t.requisitos = [_aplicar_requisito(Requisito(), r) for r in dados.requisitos]
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


def _avaliar_par(f: Funcionario, req, janela: Janela, comps, contrato) -> tuple[Avaliacao, list[str]]:
    av = avaliar(f, efetivo(req, contrato), janela, comps[f.id], remanejar=False)
    motivos = list(av.motivos)
    if norm(f.papel) != norm(req.papel):
        motivos.insert(0, f"papel '{f.papel}' diferente do pedido '{req.papel}'")
    return av, motivos


def confirmar(
    db: Session, t: Tarefa, escolhas: list[schemas.Escolha] | None = None, forcar: bool = False,
    motivo: str | None = None, autor: str = "sistema",
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

    janela = Janela.de(t)
    contrato = contrato_por_codigo(db, t.contrato)
    comps = carregar_compromissos(db, janela, ids)
    resultados, problemas = [], {}
    for rid, fid in pares:
        av, motivos = _avaliar_par(funcs[fid], reqs[rid], janela, comps, contrato)
        if motivos:
            problemas[funcs[fid].nome] = motivos
        resultados.append((rid, funcs[fid], av, motivos))

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
            just.update(forcado=True, violacoes=motivos, forcado_por=autor, motivo_forcar=motivo)
        a = Alocacao(
            requisito_id=rid, funcionario_id=f.id, funcionario=f,
            status="confirmada", score=av.score, justificativa=just,
        )
        t.alocacoes.append(a)
        novas.append((a, f, motivos))
    atualizar_status(t)
    db.flush()  # ids das alocações, usados nos avisos

    for a, f, motivos in novas:
        papel = reqs[a.requisito_id].papel
        if motivos:
            auditar(db, "forcar", f"{f.nome} confirmado(a) na #{t.id} ({papel}) contra as regras"
                    + (f": {motivo}" if motivo else " (sem motivo informado)"),
                    autor, t.id, f.id, {"violacoes": motivos, "motivo": motivo})
        else:
            auditar(db, "confirmar", f"{f.nome} confirmado(a) na #{t.id} ({papel})", autor, t.id, f.id)
        _avisar(db, f, "alocado", t, f"Você foi escalado(a) como {papel}:\n{_resumo_tarefa(t)}", a)
    db.commit()
    return [a for a, _, _ in novas]


def obter_alocacao(db: Session, alocacao_id: int) -> Alocacao:
    a = db.get(Alocacao, alocacao_id)
    if a is None:
        raise ErroNegocio(f"Alocação #{alocacao_id} não encontrada", 404)
    return a


def cancelar_alocacao(db: Session, a: Alocacao, autor: str = "sistema") -> Alocacao:
    t = a.tarefa
    _exigir_ativa(t)  # histórico de tarefa concluída ou cancelada não muda
    if a.status == "cancelada":
        raise ErroNegocio(f"A alocação #{a.id} já está cancelada", 409)
    era_confirmada = a.status == "confirmada"
    a.status = "cancelada"
    atualizar_status(t)
    if era_confirmada:
        auditar(db, "cancelar_alocacao", f"{a.funcionario_nome} saiu da #{t.id}", autor, t.id, a.funcionario_id)
        _avisar(db, a.funcionario, "removido", t, f"Você não está mais escalado(a) em:\n{_resumo_tarefa(t)}", a)
    db.commit()
    return a


def substitutos(db: Session, a: Alocacao, n: int = 3) -> tuple[list[Avaliacao], Vaga | None]:
    """As `n` melhores pessoas para a vaga de `a`, como se `a` já tivesse sido cancelada."""
    prop = propor(db, a.tarefa, max_alternativas=n, excluir_funcionarios={a.funcionario_id}, ignorar_alocacoes={a.id})
    vaga = next((v for v in prop.vagas if v.requisito.id == a.requisito_id), None)
    if vaga is None:
        return [], None
    return (vaga.escolhidos[:1] + vaga.alternativas)[:n], vaga


def sugerir_substituto(db: Session, a: Alocacao) -> tuple[Avaliacao | None, Vaga | None]:
    opcoes, vaga = substitutos(db, a, 1)
    return (opcoes[0] if opcoes else None), vaga


def avaliar_troca(db: Session, a: Alocacao, novo_funcionario_id: int) -> tuple[Funcionario, Avaliacao, list[str]]:
    """Como `novo` se sairia na vaga de `a` (sem gravar nada)."""
    t, f = a.tarefa, obter_funcionario(db, novo_funcionario_id)
    req = next((r for r in t.requisitos if r.id == a.requisito_id), None)
    if req is None:
        raise ErroNegocio(f"A vaga da alocação #{a.id} não existe mais", 409)
    janela = Janela.de(t)
    av, motivos = _avaliar_par(f, req, janela, carregar_compromissos(db, janela, [f.id]),
                               contrato_por_codigo(db, t.contrato))
    if any(x.funcionario_id == f.id and x.status == "confirmada" for x in t.alocacoes):
        motivos.insert(0, "já está confirmado(a) nesta tarefa")
    return f, av, motivos


def substituir(db: Session, a: Alocacao, novo_funcionario_id: int, autor: str = "sistema") -> Alocacao:
    """Cancela `a` e confirma outra pessoa na mesma vaga (regras checadas de novo)."""
    if a.status != "confirmada":
        raise ErroNegocio(f"A alocação #{a.id} não está confirmada", 409)
    _exigir_ativa(a.tarefa)
    a.status = "cancelada"
    try:
        [nova] = confirmar(db, a.tarefa, [schemas.Escolha(requisito_id=a.requisito_id, funcionario_id=novo_funcionario_id)],
                           autor=autor)
    except ErroNegocio:
        db.rollback()
        raise
    t = a.tarefa
    auditar(db, "substituir", f"{nova.funcionario_nome} entrou no lugar de {a.funcionario_nome} na #{t.id}",
            autor, t.id, nova.funcionario_id, {"saiu": a.funcionario_id})
    _avisar(db, a.funcionario, "removido", t, f"Você foi substituído(a) em:\n{_resumo_tarefa(t)}", a)
    db.commit()
    return nova


def encerrar_tarefa(db: Session, t: Tarefa, status: str, autor: str = "sistema") -> Tarefa:
    assert status in ENCERRADAS
    _exigir_ativa(t)
    t.status = status
    if status == "cancelada":
        for a in t.alocacoes:
            if a.status == "confirmada":
                _avisar(db, a.funcionario, "removido", t, f"Tarefa cancelada:\n{_resumo_tarefa(t)}", a)
            if a.status in ("proposta", "confirmada"):
                a.status = "cancelada"
    else:
        t.alocacoes = [a for a in t.alocacoes if a.status != "proposta"]
    auditar(db, "encerrar", f"#{t.id} {t.titulo} marcada como {status}", autor, t.id)
    db.commit()
    return t


def revalidar_confirmadas(db: Session, t: Tarefa, janela: Janela | None = None) -> list[tuple[Alocacao, list[str]]]:
    """Confirmados que hoje (ou no período `janela`) violam alguma regra: ausência nova, horário mudou..."""
    janela = janela or Janela.de(t)
    confirmadas = [a for a in t.alocacoes if a.status == "confirmada"]
    if not confirmadas:
        return []
    reqs = {r.id: r for r in t.requisitos}
    contrato = contrato_por_codigo(db, t.contrato)
    comps = carregar_compromissos(db, janela, [a.funcionario_id for a in confirmadas])
    out = []
    for a in confirmadas:
        req = reqs.get(a.requisito_id)
        motivos = ["a vaga foi removida"] if req is None else _avaliar_par(a.funcionario, req, janela, comps, contrato)[1]
        if motivos:
            out.append((a, motivos))
    return out


def simular_remarcacao(db: Session, t: Tarefa, inicio: datetime, fim: datetime) -> list[tuple[Alocacao, list[str]]]:
    if fim <= inicio:
        raise ErroNegocio("fim deve ser depois do início")
    return revalidar_confirmadas(db, t, replace(Janela.de(t), inicio=inicio, fim=fim))


def _apos_mudar_periodo(db: Session, t: Tarefa, antes: tuple, autor: str, acao: str) -> list[tuple[Alocacao, list[str]]]:
    t.alocacoes = [a for a in t.alocacoes if a.status != "proposta"]  # propostas antigas não valem mais
    db.flush()
    problemas = revalidar_confirmadas(db, t)
    atualizar_status(t)
    if (t.inicio, t.fim) != antes[:2]:
        for a in t.alocacoes:
            if a.status == "confirmada":
                _avisar(db, a.funcionario, "remarcado", t, f"Mudou o horário de:\n{_resumo_tarefa(t)}", a)
    auditar(db, acao, f"#{t.id} {t.titulo}: {quando(*antes[:2])} → {quando(t.inicio, t.fim)}"
            if (t.inicio, t.fim) != antes[:2] else f"#{t.id} {t.titulo} editada", autor, t.id,
            detalhes={"problemas": {a.funcionario_nome: m for a, m in problemas}} if problemas else None)
    db.commit()
    return problemas


def remarcar_tarefa(db: Session, t: Tarefa, inicio: datetime, fim: datetime, autor: str = "sistema"):
    """Muda o período. Não tira ninguém sozinho: devolve quem passou a violar regras."""
    _exigir_ativa(t)
    if fim <= inicio:
        raise ErroNegocio("fim deve ser depois do início")
    antes = (t.inicio, t.fim)
    t.inicio, t.fim = inicio, fim
    return _apos_mudar_periodo(db, t, antes, autor, "remarcar")


def editar_tarefa(db: Session, t: Tarefa, dados: schemas.TarefaIn, autor: str = "sistema"):
    """Atualiza dados, período e vagas (vagas existentes vêm com `id`). Propostas pendentes são descartadas.
    Não dá para remover ou trocar a função de uma vaga com gente confirmada, nem pedir menos gente do que
    já está confirmado. Devolve os confirmados que passaram a violar regras (não cancela nada sozinha)."""
    _exigir_ativa(t)
    existentes = {r.id: r for r in t.requisitos}
    conf = Counter(a.requisito_id for a in t.alocacoes if a.status == "confirmada")
    novos, mantidos = [], set()
    for r in dados.requisitos:
        if r.id is None:
            novos.append(_aplicar_requisito(Requisito(), r))
            continue
        req = existentes.get(r.id)
        if req is None:
            raise ErroNegocio(f"A vaga #{r.id} não pertence à tarefa #{t.id}", 400)
        if r.quantidade < conf[r.id]:
            raise ErroNegocio(f"A vaga de {req.papel} já tem {conf[r.id]} confirmado(s); "
                              f"tire alguém antes de reduzir para {r.quantidade}", 409)
        if conf[r.id] and norm(r.papel) != norm(req.papel):
            raise ErroNegocio(f"A vaga de {req.papel} tem gente confirmada; não dá para trocar a função", 409)
        mantidos.add(r.id)
        novos.append(_aplicar_requisito(req, r))
    for rid, req in existentes.items():
        if rid not in mantidos and conf[rid]:
            raise ErroNegocio(f"A vaga de {req.papel} tem gente confirmada; tire as pessoas antes de remover a vaga", 409)

    antes = (t.inicio, t.fim)
    for campo in ("titulo", "descricao", "tipo", "local", "inicio", "fim", "horas_por_pessoa", "prioridade"):
        setattr(t, campo, getattr(dados, campo))
    t.contrato = (dados.contrato or "").strip() or None
    for a in t.alocacoes:  # histórico de vagas removidas fica sem vaga
        if a.requisito_id is not None and a.requisito_id in existentes and a.requisito_id not in mantidos:
            a.requisito_id = None
    t.requisitos = novos
    return _apos_mudar_periodo(db, t, antes, autor, "editar_tarefa")


# ---------- Visão da semana ----------


def grade_semana(db: Session, segunda: date, papel: str | None = None) -> dict:
    """Funcionários × dias: tarefas (confirmadas e propostas), ausências e horas da semana."""
    ini = datetime.combine(segunda, time.min)
    fim = ini + timedelta(days=7)
    dias = [segunda + timedelta(days=i) for i in range(7)]
    funcs = listar_funcionarios(db, papel=papel)
    ids = [f.id for f in funcs]
    alocs = db.scalars(
        select(Alocacao).join(Tarefa, Alocacao.tarefa_id == Tarefa.id).options(contains_eager(Alocacao.tarefa))
        .where(Alocacao.funcionario_id.in_(ids), Alocacao.status.in_(("confirmada", "proposta")),
               Tarefa.status != "cancelada", Tarefa.inicio < fim, Tarefa.fim > ini)
        .order_by(Tarefa.inicio)
    ).unique().all() if ids else []
    indis = db.scalars(select(Indisponibilidade).where(
        Indisponibilidade.funcionario_id.in_(ids), Indisponibilidade.inicio < fim, Indisponibilidade.fim > ini,
    )).all() if ids else []

    por_func = defaultdict(list)
    for a in alocs:
        por_func[a.funcionario_id].append(a)
    aus_func = defaultdict(list)
    for i in indis:
        aus_func[i.funcionario_id].append(i)

    linhas = []
    for f in funcs:
        celulas = [[] for _ in dias]
        horas, previstas = [0.0] * 7, 0.0
        for a in por_func[f.id]:
            t = a.tarefa
            hd = horas_por_dia(t.inicio, t.fim, t.horas_por_pessoa)
            for k, d in enumerate(dias):
                if sobrepoe(t.inicio, t.fim, datetime.combine(d, time.min), datetime.combine(d, time.min) + timedelta(days=1)):
                    celulas[k].append({"tipo": "tarefa", "status": a.status, "id": t.id, "titulo": t.titulo,
                                       "inicio": t.inicio, "fim": t.fim, "campo": t.tipo == "campo",
                                       "horas": round(hd.get(d, 0.0), 1)})
                    if a.status == "confirmada":
                        horas[k] += hd.get(d, 0.0)
                    else:
                        previstas += hd.get(d, 0.0)
        for i in aus_func[f.id]:
            for k, d in enumerate(dias):
                if sobrepoe(i.inicio, i.fim, datetime.combine(d, time.min), datetime.combine(d, time.min) + timedelta(days=1)):
                    celulas[k].insert(0, {"tipo": "ausencia", "motivo": i.motivo})
        total = sum(horas)
        linhas.append({
            "f": f, "celulas": celulas, "horas": [round(h, 1) for h in horas], "total": round(total, 1),
            "previstas": round(previstas, 1), "pct": round(100 * total / f.carga_horaria_semanal),
            "excede_dia": [h > f.horas_max_dia + 1e-6 for h in horas],
        })
    return {"dias": dias, "linhas": linhas}


# ---------- Contratos ----------


def listar_contratos(db: Session) -> list[Contrato]:
    return list(db.scalars(select(Contrato).order_by(Contrato.codigo)).all())


def obter_contrato(db: Session, contrato_id: int) -> Contrato:
    c = db.get(Contrato, contrato_id)
    if c is None:
        raise ErroNegocio(f"Contrato #{contrato_id} não encontrado", 404)
    return c


def salvar_contrato(db: Session, dados: schemas.ContratoIn, c: Contrato | None = None, autor: str = "sistema") -> Contrato:
    outro = contrato_por_codigo(db, dados.codigo)
    if outro and (c is None or outro.id != c.id):
        raise ErroNegocio(f"Já existe um contrato com o código {dados.codigo}", 409)
    novo = c is None
    c = c or Contrato()
    for campo in ("codigo", "cliente", "certificacoes", "anos_experiencia_min", "exige_tecnico_seguranca",
                  "observacoes", "texto"):
        setattr(c, campo, getattr(dados, campo))
    if novo:
        db.add(c)
    auditar(db, "contrato", f"Contrato {c.codigo} {'cadastrado' if novo else 'atualizado'}", autor,
            detalhes={"certificacoes": c.certificacoes, "anos_experiencia_min": c.anos_experiencia_min,
                      "exige_tecnico_seguranca": c.exige_tecnico_seguranca})
    db.commit()
    return c


def remover_contrato(db: Session, c: Contrato, autor: str = "sistema") -> None:
    auditar(db, "contrato", f"Contrato {c.codigo} removido", autor)
    db.delete(c)
    db.commit()


def avisos_contrato(t: Tarefa, contrato: Contrato | None) -> list[str]:
    """Exigências do contrato que a montagem da tarefa não cobre (o motor já aplica certificações e experiência)."""
    if contrato is None:
        return []
    avisos = []
    if contrato.exige_tecnico_seguranca and not any("seguranca" in norm(r.papel) for r in t.requisitos):
        avisos.append(f"O contrato {contrato.codigo} exige técnico de segurança, e a tarefa não tem essa vaga.")
    return avisos


# ---------- Avisos para a equipe (caixa de saída do bot) ----------


def notificacoes_pendentes(db: Session, limite: int = 30) -> list[Notificacao]:
    """Pendentes, já descartando as velhas (mais de 2 dias) e as de tarefas que já acabaram."""
    pend = db.scalars(select(Notificacao).where(Notificacao.situacao == "pendente")
                      .order_by(Notificacao.id).limit(limite)).all()
    validas, agora_ = [], agora()
    for n in pend:
        t = db.get(Tarefa, n.tarefa_id) if n.tarefa_id else None
        if n.criada_em < agora_ - timedelta(days=2) or (t is not None and t.fim < agora_):
            n.situacao = "expirada"
        else:
            validas.append(n)
    db.commit()
    return validas


def marcar_notificacao(db: Session, notificacao_id: int, ok: bool) -> None:
    n = db.get(Notificacao, notificacao_id)
    if n:
        n.situacao, n.enviada_em = ("enviada" if ok else "falhou"), agora()
        db.commit()


def responder_notificacao(
    db: Session, notificacao_id: int, telegram_id: int | str, resposta: str
) -> tuple[Notificacao, Alocacao | None]:
    """Grava 'ciente' ou 'imprevisto'. Devolve a alocação se ela ainda vale (para procurar substituto)."""
    n = db.get(Notificacao, notificacao_id)
    if n is None or str(n.funcionario.telegram_id) != str(telegram_id):
        raise ErroNegocio("Aviso não encontrado", 404)
    if n.resposta:
        raise ErroNegocio(f"Você já respondeu: {n.resposta}", 409)
    n.resposta, n.respondida_em = resposta, agora()
    auditar(db, "aviso_resposta", f"{n.funcionario.nome} respondeu '{resposta}' ao aviso da #{n.tarefa_id}",
            f"telegram:{n.funcionario.nome}", n.tarefa_id, n.funcionario_id)
    db.commit()
    a = db.get(Alocacao, n.alocacao_id) if n.alocacao_id else None
    if a is None or a.status != "confirmada" or a.tarefa.status in ENCERRADAS:
        a = None
    return n, a


# ---------- Ausências informadas pelo próprio funcionário ----------


def criar_solicitacao(db: Session, f: Funcionario, inicio: datetime, fim: datetime, motivo: str) -> SolicitacaoAusencia:
    if fim <= inicio:
        raise ErroNegocio("fim deve ser depois do início")
    s = SolicitacaoAusencia(funcionario_id=f.id, funcionario=f, inicio=inicio, fim=fim, motivo=motivo or "outro")
    db.add(s)
    db.flush()
    auditar(db, "solicitacao", f"{f.nome} pediu ausência ({s.motivo}): {quando(inicio, fim)}",
            f"telegram:{f.nome}", funcionario_id=f.id)
    db.commit()
    return s


def solicitacoes(db: Session, status: str | None = "pendente") -> list[SolicitacaoAusencia]:
    q = select(SolicitacaoAusencia).order_by(SolicitacaoAusencia.criada_em.desc())
    if status:
        q = q.where(SolicitacaoAusencia.status == status)
    return list(db.scalars(q).all())


def decidir_solicitacao(db: Session, s: SolicitacaoAusencia, aprovar: bool, autor: str = "sistema") -> list[Alocacao]:
    """Aprovar registra a ausência e devolve as alocações confirmadas que ela atinge."""
    if s.status != "pendente":
        raise ErroNegocio(f"Esse pedido já foi {s.status}", 409)
    s.status, s.decidida_em, s.decidida_por = ("aprovada" if aprovar else "recusada"), agora(), autor
    if not aprovar:
        auditar(db, "solicitacao", f"Pedido de ausência de {s.funcionario.nome} recusado", autor,
                funcionario_id=s.funcionario_id)
        db.commit()
        return []
    _, afetadas = criar_indisponibilidade(
        db, s.funcionario, schemas.IndisponibilidadeIn(inicio=s.inicio, fim=s.fim, motivo=s.motivo), autor
    )
    return afetadas


# ---------- Resumo semanal ----------


def resumo_semanal_devido(db: Session) -> bool:
    """A partir de segunda 08h, uma vez por semana."""
    agora_ = agora()
    seg = datetime.combine(agora_.date() - timedelta(days=agora_.weekday()), time(8))
    if agora_ < seg:
        return False
    return not db.scalar(select(Auditoria.id).where(Auditoria.acao == "resumo_semanal", Auditoria.quando >= seg))


def marcar_resumo_semanal(db: Session, texto: str) -> None:
    auditar(db, "resumo_semanal", texto.split("\n", 1)[0], "bot")
    db.commit()


def ausencias_proximas(db: Session, dias: int = 14) -> list[Indisponibilidade]:
    return list(db.scalars(
        select(Indisponibilidade).options(joinedload(Indisponibilidade.funcionario))
        .where(Indisponibilidade.fim > agora(),
               Indisponibilidade.inicio < datetime.combine(hoje() + timedelta(days=dias), time.max))
        .order_by(Indisponibilidade.inicio)
    ).all())
