"""Painel web simples (HTML renderizado no servidor). Usa os mesmos serviços da API."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import parsing, schemas
from . import services as sv
from .allocation import disponiveis, propor
from .db import get_db
from .models import Indisponibilidade

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["dt"] = lambda v: v.strftime("%d/%m/%Y %H:%M") if v else ""
templates.env.filters["d"] = lambda v: v.strftime("%d/%m/%Y") if v else ""
templates.env.filters["dtinput"] = lambda v: v.strftime("%Y-%m-%dT%H:%M") if v else ""

STATUS_LABEL = {
    "aberta": "Aberta", "proposta": "Proposta pendente", "parcial": "Parcial",
    "alocada": "Alocada", "concluida": "Concluída", "cancelada": "Cancelada",
}
templates.env.globals["STATUS_LABEL"] = STATUS_LABEL


# ---------- utilidades de formulário ----------


_itens = parsing.itens
parse_especialidades = parsing.especialidades
parse_certificacoes = parsing.certificacoes


def _num(valor: str | None, padrao=None):
    valor = (valor or "").strip().replace(",", ".")
    return float(valor) if valor else padrao


def _txt(valor: str | None) -> str | None:
    valor = (valor or "").strip()
    return valor or None


def _erro_validacao(e: Exception) -> str:
    if isinstance(e, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'dados'}: {err['msg']}" for err in e.errors()
        )
    return str(e)


def _redir(url: str, msg: str | None = None, erro: str | None = None) -> RedirectResponse:
    params = {k: v for k, v in (("msg", msg), ("erro", erro)) if v}
    return RedirectResponse(f"{url}?{urlencode(params)}" if params else url, status_code=303)


def _ctx(request: Request, **kw) -> dict:
    return {"request": request, "msg": request.query_params.get("msg"), "erro": request.query_params.get("erro"), **kw}


def _funcionario_de_form(form) -> schemas.FuncionarioIn:
    return schemas.FuncionarioIn(
        nome=(form.get("nome") or "").strip(),
        papel=(form.get("papel") or "").strip(),
        email=_txt(form.get("email")),
        telegram_id=_txt(form.get("telegram_id")),
        carga_horaria_semanal=_num(form.get("carga_horaria_semanal"), 44),
        horas_max_dia=_num(form.get("horas_max_dia"), 8),
        anos_experiencia=_num(form.get("anos_experiencia"), 0),
        disponivel_viagem=form.get("disponivel_viagem") == "on",
        base=_txt(form.get("base")),
        resumo=_txt(form.get("resumo")),
        especialidades=parse_especialidades(form.get("especialidades"), "nivel", 3),
        certificacoes=parse_certificacoes(form.get("certificacoes")),
    )


def _papeis(db: Session) -> list[str]:
    return sorted({f.papel for f in sv.listar_funcionarios(db, incluir_inativos=True)})


# ---------- páginas ----------


@router.get("/", response_class=HTMLResponse)
def inicio(request: Request, db: Session = Depends(get_db)):
    tarefas = sv.listar_tarefas(db)
    ativas = sorted((t for t in tarefas if t.status not in sv.ENCERRADAS), key=lambda t: t.inicio)
    hoje = date.today()
    livres_hoje = disponiveis(db, datetime.combine(hoje, time(8)), datetime.combine(hoje, time(17)))
    ausencias = db.scalars(
        select(Indisponibilidade)
        .where(
            Indisponibilidade.fim > datetime.now(),
            Indisponibilidade.inicio < datetime.combine(hoje + timedelta(days=14), time.max),
        )
        .order_by(Indisponibilidade.inicio)
    ).all()
    return templates.TemplateResponse(
        request, "index.html",
        _ctx(request, ativas=ativas, livres_hoje=livres_hoje, ausencias=ausencias,
             total_funcionarios=len(sv.listar_funcionarios(db))),
    )


@router.get("/funcionarios", response_class=HTMLResponse)
def funcionarios(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "funcionarios.html",
        _ctx(request, funcionarios=sv.listar_funcionarios(db, incluir_inativos=True), papeis=_papeis(db), form={}),
    )


@router.post("/funcionarios", response_class=HTMLResponse)
async def criar_funcionario(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        f = sv.criar_funcionario(db, _funcionario_de_form(form))
    except (ValidationError, ValueError) as e:
        return templates.TemplateResponse(
            request, "funcionarios.html",
            _ctx(request, funcionarios=sv.listar_funcionarios(db, incluir_inativos=True), papeis=_papeis(db),
                 form=dict(form), erro=_erro_validacao(e)),
            status_code=400,
        )
    return _redir(f"/funcionarios/{f.id}", msg=f"{f.nome} cadastrado(a)")


@router.get("/funcionarios/{funcionario_id}", response_class=HTMLResponse)
def funcionario(funcionario_id: int, request: Request, db: Session = Depends(get_db)):
    f = sv.obter_funcionario(db, funcionario_id)
    hoje = datetime.combine(date.today(), time.min)
    eventos = sv.agenda(db, f, hoje, hoje + timedelta(days=45))
    valores = {
        c: getattr(f, c)
        for c in ("nome", "papel", "email", "telegram_id", "carga_horaria_semanal", "horas_max_dia",
                  "anos_experiencia", "base", "resumo", "disponivel_viagem")
    }
    valores["especialidades"] = ", ".join(f"{e.nome}:{e.nivel}" for e in f.especialidades)
    valores["certificacoes"] = ", ".join(
        f"{c.nome}:{c.validade.isoformat()}" if c.validade else c.nome for c in f.certificacoes
    )
    return templates.TemplateResponse(
        request, "funcionario.html",
        _ctx(request, f=f, eventos=eventos, papeis=_papeis(db), valores=valores,
             indisponibilidades=sorted(f.indisponibilidades, key=lambda i: i.inicio, reverse=True)),
    )


@router.post("/funcionarios/{funcionario_id}")
async def atualizar_funcionario(funcionario_id: int, request: Request, db: Session = Depends(get_db)):
    f = sv.obter_funcionario(db, funcionario_id)
    try:
        sv.atualizar_funcionario(db, f, _funcionario_de_form(await request.form()))
    except (ValidationError, ValueError) as e:
        return _redir(f"/funcionarios/{f.id}", erro=_erro_validacao(e))
    return _redir(f"/funcionarios/{f.id}", msg="Cadastro atualizado")


@router.post("/funcionarios/{funcionario_id}/ativo")
async def alternar_ativo(funcionario_id: int, db: Session = Depends(get_db)):
    f = sv.obter_funcionario(db, funcionario_id)
    f.ativo = not f.ativo
    db.commit()
    return _redir(f"/funcionarios/{f.id}", msg="Reativado" if f.ativo else "Desativado")


@router.post("/funcionarios/{funcionario_id}/indisponibilidades")
async def criar_indisponibilidade(funcionario_id: int, request: Request, db: Session = Depends(get_db)):
    f = sv.obter_funcionario(db, funcionario_id)
    form = await request.form()
    try:
        dados = schemas.IndisponibilidadeIn(
            inicio=form.get("inicio"), fim=form.get("fim"), motivo=(form.get("motivo") or "outro").strip()
        )
    except ValidationError as e:
        return _redir(f"/funcionarios/{f.id}", erro=_erro_validacao(e))
    _, afetadas = sv.criar_indisponibilidade(db, f, dados)
    if afetadas:
        lista = ", ".join(f"#{a.tarefa.id} {a.tarefa.titulo}" for a in afetadas)
        return _redir(f"/funcionarios/{f.id}", erro=f"Ausência registrada, mas atinge tarefas já confirmadas: {lista}")
    return _redir(f"/funcionarios/{f.id}", msg="Ausência registrada")


@router.post("/indisponibilidades/{indisponibilidade_id}/remover")
def remover_indisponibilidade(indisponibilidade_id: int, db: Session = Depends(get_db)):
    ind = db.get(Indisponibilidade, indisponibilidade_id)
    destino = f"/funcionarios/{ind.funcionario_id}" if ind else "/funcionarios"
    sv.remover_indisponibilidade(db, indisponibilidade_id)
    return _redir(destino, msg="Ausência removida")


@router.get("/tarefas/nova", response_class=HTMLResponse)
def nova_tarefa(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "tarefa_nova.html", _ctx(request, form={}, papeis=_papeis(db)))


@router.post("/tarefas", response_class=HTMLResponse)
async def criar_tarefa(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        requisitos = []
        for i in range(3):
            papel = (form.get(f"req_papel_{i}") or "").strip()
            if not papel:
                continue
            requisitos.append({
                "papel": papel,
                "quantidade": int(_num(form.get(f"req_qtd_{i}"), 1)),
                "especialidades": parse_especialidades(form.get(f"req_esp_{i}"), "nivel_min", 1),
                "certificacoes": _itens(form.get(f"req_cert_{i}")),
                "desejaveis": _itens(form.get(f"req_des_{i}")),
                "anos_experiencia_min": _num(form.get(f"req_exp_{i}"), 0),
            })
        dados = schemas.TarefaIn(
            titulo=(form.get("titulo") or "").strip(),
            descricao=_txt(form.get("descricao")),
            tipo=form.get("tipo") or "interna",
            local=_txt(form.get("local")),
            inicio=form.get("inicio"),
            fim=form.get("fim"),
            horas_por_pessoa=_num(form.get("horas_por_pessoa")),
            prioridade=int(_num(form.get("prioridade"), 3)),
            contrato=_txt(form.get("contrato")),
            requisitos=requisitos,
        )
        t = sv.criar_tarefa(db, dados)
    except (ValidationError, ValueError) as e:
        return templates.TemplateResponse(
            request, "tarefa_nova.html",
            _ctx(request, form=dict(form), papeis=_papeis(db), erro=_erro_validacao(e)),
            status_code=400,
        )
    return _redir(f"/tarefas/{t.id}", msg="Tarefa criada. Gere a proposta de alocação.")


@router.get("/tarefas/{tarefa_id}", response_class=HTMLResponse)
def tarefa(tarefa_id: int, request: Request, db: Session = Depends(get_db)):
    t = sv.obter_tarefa(db, tarefa_id)
    ativa = t.status not in sv.ENCERRADAS
    vagas = {v.requisito.id: v for v in propor(db, t).vagas} if ativa else {}
    linhas = []
    for r in t.requisitos:
        gravadas = [a for a in t.alocacoes if a.requisito_id == r.id and a.status != "cancelada"]
        ids = {a.funcionario_id for a in gravadas}
        vaga = vagas.get(r.id)
        viaveis = [a for a in (vaga.escolhidos + vaga.alternativas) if a.funcionario.id not in ids] if vaga else []
        linhas.append({
            "r": r,
            "gravadas": gravadas,
            "viaveis": viaveis,
            "rejeitados": vaga.rejeitados if vaga else [],
            "faltam": max(0, r.quantidade - sum(a.status == "confirmada" for a in gravadas)),
        })
    tem_proposta = any(a.status == "proposta" for a in t.alocacoes)
    return templates.TemplateResponse(
        request, "tarefa.html", _ctx(request, t=t, ativa=ativa, linhas=linhas, tem_proposta=tem_proposta)
    )


def _acao_tarefa(tarefa_id: int, db: Session, fn, msg: str):
    t = sv.obter_tarefa(db, tarefa_id)
    try:
        fn(t)
    except sv.ErroNegocio as e:
        detalhe = ""
        if e.problemas:
            detalhe = " | " + " | ".join(f"{nome}: {'; '.join(m)}" for nome, m in e.problemas.items())
        return _redir(f"/tarefas/{t.id}", erro=e.mensagem + detalhe)
    return _redir(f"/tarefas/{t.id}", msg=msg)


@router.post("/tarefas/{tarefa_id}/propor")
def web_propor(tarefa_id: int, db: Session = Depends(get_db)):
    return _acao_tarefa(tarefa_id, db, lambda t: sv.gerar_proposta(db, t), "Proposta gerada")


@router.post("/tarefas/{tarefa_id}/confirmar")
async def web_confirmar(tarefa_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    escolhas, forcar = None, form.get("forcar") == "1"
    if form.get("funcionario_id"):
        escolhas = [schemas.Escolha(requisito_id=int(form["requisito_id"]), funcionario_id=int(form["funcionario_id"]))]
    return _acao_tarefa(tarefa_id, db, lambda t: sv.confirmar(db, t, escolhas, forcar), "Alocação confirmada")


@router.post("/tarefas/{tarefa_id}/concluir")
def web_concluir(tarefa_id: int, db: Session = Depends(get_db)):
    return _acao_tarefa(tarefa_id, db, lambda t: sv.encerrar_tarefa(db, t, "concluida"), "Tarefa concluída")


@router.post("/tarefas/{tarefa_id}/cancelar")
def web_cancelar(tarefa_id: int, db: Session = Depends(get_db)):
    return _acao_tarefa(tarefa_id, db, lambda t: sv.encerrar_tarefa(db, t, "cancelada"), "Tarefa cancelada")


@router.post("/alocacoes/{alocacao_id}/cancelar")
def web_cancelar_alocacao(alocacao_id: int, db: Session = Depends(get_db)):
    a = sv.obter_alocacao(db, alocacao_id)
    sv.cancelar_alocacao(db, a)
    return _redir(f"/tarefas/{a.tarefa_id}", msg=f"Alocação de {a.funcionario_nome} cancelada")
