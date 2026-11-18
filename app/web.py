"""Painel web simples (HTML renderizado no servidor). Usa os mesmos serviços da API."""

from __future__ import annotations

import csv
import hmac
import io
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from jinja2 import pass_context
from pydantic import ValidationError
from sqlalchemy.orm import Session

from . import config, contratos, parsing, provedores, schemas, seguranca
from . import services as sv
from .allocation import disponiveis, horas_por_dia, propor, segunda_da_semana
from .db import get_db
from .models import Indisponibilidade, SolicitacaoAusencia
from .relogio import agora, hoje, para_utc

AUTOR = "painel"
SESSAO_DIAS = 7


async def verificar_csrf(request: Request) -> None:
    """Todo POST do painel leva o token do cookie `csrf` num campo do formulário (padrão double-submit).
    Um site de fora não lê o cookie, então não consegue montar um formulário válido."""
    if request.method != "POST":
        return
    origem = request.headers.get("origin")
    if origem and origem != "null" and urlparse(origem).netloc != request.url.netloc:
        raise sv.ErroNegocio("Pedido vindo de outro site foi bloqueado.", 403)
    form = await request.form()
    token, cookie = str(form.get("csrf") or ""), request.cookies.get("csrf") or ""
    if not cookie or not hmac.compare_digest(token.encode(), cookie.encode()):
        raise sv.ErroNegocio("Formulário expirado. Recarregue a página e tente de novo.", 403)


router = APIRouter(include_in_schema=False, dependencies=[Depends(verificar_csrf)])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["dt"] = lambda v: v.strftime("%d/%m/%Y %H:%M") if v else ""
templates.env.filters["d"] = lambda v: v.strftime("%d/%m/%Y") if v else ""
templates.env.filters["dm"] = lambda v: v.strftime("%d/%m") if v else ""
templates.env.filters["hm"] = lambda v: v.strftime("%H:%M") if v else ""
templates.env.filters["dtinput"] = lambda v: v.strftime("%Y-%m-%dT%H:%M") if v else ""
templates.env.filters["h"] = lambda v: f"{round(v or 0, 1):g}"

STATUS_LABEL = {
    "aberta": "Aberta", "proposta": "Proposta pendente", "parcial": "Parcial",
    "alocada": "Alocada", "concluida": "Concluída", "cancelada": "Cancelada",
}
DIAS_SEMANA = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
templates.env.globals.update(STATUS_LABEL=STATUS_LABEL, DIAS_SEMANA=DIAS_SEMANA)


@pass_context
def _campo_csrf(ctx) -> Markup:
    token = getattr(ctx["request"].state, "csrf", "")
    return Markup(f'<input type="hidden" name="csrf" value="{token}">')


templates.env.globals["csrf"] = _campo_csrf


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
    if isinstance(e, sv.ErroNegocio):
        return e.mensagem
    return str(e)


def _redir(url: str, msg: str | None = None, erro: str | None = None) -> RedirectResponse:
    """O aviso vai num cookie assinado e de vida curta, não na URL: F5 não repete e ninguém forja o texto."""
    r = RedirectResponse(url, status_code=303)
    if msg or erro:
        dados = {k: v[:1500] for k, v in (("msg", msg), ("erro", erro)) if v}
        r.set_cookie("flash", seguranca.assinar(dados, 120), max_age=120, httponly=True, samesite="lax")
    return r


def _ctx(request: Request, **kw) -> dict:
    flash = getattr(request.state, "flash", {}) or {}
    return {"request": request, "msg": flash.get("msg"), "erro": flash.get("erro"), **kw}


def _form_dict(form) -> dict:
    """Valores do formulário para re-exibir após erro. Checkbox desmarcado não vem no POST: vira False."""
    valores = dict(form)
    valores["disponivel_viagem"] = form.get("disponivel_viagem") == "on"
    valores.pop("csrf", None)
    return valores


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


def _indices_req(form) -> list[int]:
    out = set()
    for k in form.keys():
        if k.startswith("req_papel_") and k.removeprefix("req_papel_").isdigit():
            out.add(int(k.removeprefix("req_papel_")))
    return sorted(out)


def _linhas_req_form(form) -> list[dict]:
    """Linhas de vaga como digitadas, para re-exibir o formulário após erro."""
    linhas = []
    for i in _indices_req(form):
        linhas.append({c: form.get(f"req_{c}_{i}") or "" for c in ("id", "papel", "qtd", "esp", "cert", "des", "exp")})
    return linhas or [{}]


def _linhas_req_tarefa(t) -> list[dict]:
    return [{
        "id": r.id, "papel": r.papel, "qtd": r.quantidade,
        "esp": ", ".join(f"{e['nome']}:{e.get('nivel_min', 1)}" for e in r.especialidades or []),
        "cert": ", ".join(r.certificacoes or []), "des": ", ".join(r.desejaveis or []),
        "exp": f"{r.anos_experiencia_min:g}" if r.anos_experiencia_min else "0",
    } for r in t.requisitos]


def _tarefa_de_form(form) -> schemas.TarefaIn:
    requisitos = []
    for i in _indices_req(form):
        papel = (form.get(f"req_papel_{i}") or "").strip()
        if not papel:
            continue
        rid = (form.get(f"req_id_{i}") or "").strip()
        requisitos.append({
            "id": int(rid) if rid.isdigit() else None,
            "papel": papel,
            "quantidade": int(_num(form.get(f"req_qtd_{i}"), 1)),
            "especialidades": parse_especialidades(form.get(f"req_esp_{i}"), "nivel_min", 1),
            "certificacoes": _itens(form.get(f"req_cert_{i}")),
            "desejaveis": _itens(form.get(f"req_des_{i}")),
            "anos_experiencia_min": _num(form.get(f"req_exp_{i}"), 0),
        })
    return schemas.TarefaIn(
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


def _data_param(valor: str | None) -> date | None:
    try:
        return date.fromisoformat(valor) if valor else None
    except ValueError:
        return None


def _problemas_txt(problemas) -> str:
    return " | ".join(f"{a.funcionario_nome}: {m[0]}" for a, m in problemas)


# ---------- login ----------


@router.get("/login", response_class=HTMLResponse)
def login(request: Request, proximo: str = "/"):
    if not config.PAINEL_SENHA:
        return _redir("/")
    return templates.TemplateResponse(request, "login.html", _ctx(request, proximo=proximo))


@router.post("/login")
async def fazer_login(request: Request):
    form = await request.form()
    proximo = str(form.get("proximo") or "/")
    if not proximo.startswith("/") or proximo.startswith("//"):
        proximo = "/"  # só redireciona para dentro do painel
    if not seguranca.senha_confere(form.get("senha")):
        return templates.TemplateResponse(
            request, "login.html", _ctx(request, proximo=proximo, erro="Senha incorreta."), status_code=401
        )
    r = _redir(proximo)
    r.set_cookie("sessao", seguranca.assinar({"u": "gestor"}, SESSAO_DIAS * 86400), max_age=SESSAO_DIAS * 86400,
                 httponly=True, samesite="lax", secure=request.url.scheme == "https")
    return r


@router.post("/logout")
def logout():
    r = _redir("/login" if config.PAINEL_SENHA else "/")
    r.delete_cookie("sessao")
    return r


# ---------- painel ----------


@router.get("/", response_class=HTMLResponse)
def inicio(request: Request, db: Session = Depends(get_db)):
    ativas = sv.listar_tarefas(db, status="ativas", recentes_primeiro=False)
    d = hoje()
    livres_hoje = disponiveis(db, datetime.combine(d, time(8)), datetime.combine(d, time(17)))
    return templates.TemplateResponse(
        request, "index.html",
        _ctx(request, ativas=ativas, livres_hoje=livres_hoje, ausencias=sv.ausencias_proximas(db),
             total_funcionarios=len(sv.listar_funcionarios(db)), vencimentos=sv.vencimentos(db, 30),
             solicitacoes=sv.solicitacoes(db), hoje=d),
    )


@router.post("/solicitacoes/{solicitacao_id}/{decisao}")
def decidir_solicitacao(solicitacao_id: int, decisao: str, db: Session = Depends(get_db)):
    s = db.get(SolicitacaoAusencia, solicitacao_id)
    if s is None or decisao not in ("aprovar", "recusar"):
        raise sv.ErroNegocio("Pedido não encontrado", 404)
    try:
        afetadas = sv.decidir_solicitacao(db, s, decisao == "aprovar", AUTOR)
    except sv.ErroNegocio as e:
        return _redir("/", erro=e.mensagem)
    if afetadas:
        lista = ", ".join(f"#{a.tarefa_id} {a.tarefa.titulo}" for a in afetadas)
        return _redir("/", erro=f"Ausência de {s.funcionario.nome} aprovada. Ela atinge tarefas confirmadas, "
                                f"substitua nas tarefas: {lista}")
    return _redir("/", msg=f"Pedido de {s.funcionario.nome} {'aprovado' if decisao == 'aprovar' else 'recusado'}")


# ---------- funcionários ----------


@router.get("/funcionarios", response_class=HTMLResponse)
def funcionarios(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "funcionarios.html",
        _ctx(request, funcionarios=sv.listar_funcionarios(db, incluir_inativos=True), papeis=_papeis(db), form={},
             hoje=hoje()),
    )


@router.post("/funcionarios", response_class=HTMLResponse)
async def criar_funcionario(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        f = sv.criar_funcionario(db, _funcionario_de_form(form))
    except (ValidationError, ValueError, sv.ErroNegocio) as e:
        db.rollback()
        return templates.TemplateResponse(
            request, "funcionarios.html",
            _ctx(request, funcionarios=sv.listar_funcionarios(db, incluir_inativos=True), papeis=_papeis(db),
                 form=_form_dict(form), erro=_erro_validacao(e), hoje=hoje()),
            status_code=400,
        )
    return _redir(f"/funcionarios/{f.id}", msg=f"{f.nome} cadastrado(a)")


def _valores_funcionario(f) -> dict:
    valores = {
        c: getattr(f, c)
        for c in ("nome", "papel", "email", "telegram_id", "carga_horaria_semanal", "horas_max_dia",
                  "anos_experiencia", "base", "resumo", "disponivel_viagem")
    }
    valores["especialidades"] = ", ".join(f"{e.nome}:{e.nivel}" for e in f.especialidades)
    valores["certificacoes"] = ", ".join(
        f"{c.nome}:{c.validade.isoformat()}" if c.validade else c.nome for c in f.certificacoes
    )
    return valores


def _pagina_funcionario(request: Request, db: Session, f, valores: dict | None = None, erro: str | None = None,
                        status_code: int = 200):
    inicio_d = datetime.combine(hoje(), time.min)
    eventos = sv.agenda(db, f, inicio_d, inicio_d + timedelta(days=45))
    token = seguranca.token_calendario(f.id)
    link_ics = str(request.url_for("agenda_ics", funcionario_id=f.id)) + f"?token={token}"
    ctx = _ctx(request, f=f, eventos=eventos, papeis=_papeis(db), valores=valores or _valores_funcionario(f),
               indisponibilidades=sorted(f.indisponibilidades, key=lambda i: i.inicio, reverse=True),
               futuras=sv.alocacoes_futuras(db, f) if f.ativo else [], link_ics=link_ics, hoje=hoje())
    if erro:
        ctx["erro"] = erro
    return templates.TemplateResponse(request, "funcionario.html", ctx, status_code=status_code)


@router.get("/funcionarios/{funcionario_id}", response_class=HTMLResponse)
def funcionario(funcionario_id: int, request: Request, db: Session = Depends(get_db)):
    return _pagina_funcionario(request, db, sv.obter_funcionario(db, funcionario_id))


@router.post("/funcionarios/{funcionario_id}", response_class=HTMLResponse)
async def atualizar_funcionario(funcionario_id: int, request: Request, db: Session = Depends(get_db)):
    f = sv.obter_funcionario(db, funcionario_id)
    form = await request.form()
    try:
        sv.atualizar_funcionario(db, f, _funcionario_de_form(form), AUTOR)
    except (ValidationError, ValueError, sv.ErroNegocio) as e:
        db.rollback()  # devolve a página com o que foi digitado, em vez de perder a edição
        return _pagina_funcionario(request, db, sv.obter_funcionario(db, funcionario_id), _form_dict(form),
                                   _erro_validacao(e), 400)
    return _redir(f"/funcionarios/{f.id}", msg="Cadastro atualizado")


@router.post("/funcionarios/{funcionario_id}/ativo")
async def alternar_ativo(funcionario_id: int, db: Session = Depends(get_db)):
    f = sv.obter_funcionario(db, funcionario_id)
    if f.ativo:
        futuras = sv.desativar_funcionario(db, f, forcar=True, autor=AUTOR)
        if futuras:
            lista = ", ".join(f"#{a.tarefa_id} {a.tarefa.titulo}" for a in futuras)
            return _redir(f"/funcionarios/{f.id}",
                          erro=f"Desativado, mas continua confirmado(a) em tarefas futuras. Substitua em: {lista}")
        return _redir(f"/funcionarios/{f.id}", msg="Desativado")
    try:
        sv.reativar_funcionario(db, f, AUTOR)
    except sv.ErroNegocio as e:
        return _redir(f"/funcionarios/{f.id}", erro=e.mensagem)
    return _redir(f"/funcionarios/{f.id}", msg="Reativado")


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
    _, afetadas = sv.criar_indisponibilidade(db, f, dados, AUTOR)
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


def _ics_texto(texto: str) -> str:
    return texto.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_linha(linha: str) -> str:
    """Quebra linhas longas como pede o formato iCalendar (75 bytes, continuação com espaço)."""
    dados, partes = linha.encode(), []
    while len(dados) > 75:
        corte = 75 if not partes else 74
        while corte > 0 and (dados[corte] & 0xC0) == 0x80:  # não corta caractere UTF-8 no meio
            corte -= 1
        partes.append(dados[:corte])
        dados = dados[corte:]
    partes.append(dados)
    return "\r\n ".join(p.decode() for p in partes)


def _ics_data(dt: datetime) -> str:
    return para_utc(dt).strftime("%Y%m%dT%H%M%SZ")


@router.get("/funcionarios/{funcionario_id}/agenda.ics", name="agenda_ics")
def agenda_ics(funcionario_id: int, token: str = "", db: Session = Depends(get_db)):
    """Agenda do funcionário para assinar no Google Agenda, Outlook ou celular (link com token, sem login)."""
    if not seguranca.token_calendario_ok(funcionario_id, token):
        return Response("Link inválido", status_code=403)
    f = sv.obter_funcionario(db, funcionario_id)
    inicio_d = datetime.combine(hoje() - timedelta(days=30), time.min)
    agora_utc = _ics_data(agora())
    linhas = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Alocador//Agenda//PT-BR", "CALSCALE:GREGORIAN",
              "METHOD:PUBLISH", f"X-WR-CALNAME:{_ics_texto('Agenda · ' + f.nome)}"]
    for ev in sv.agenda(db, f, inicio_d, inicio_d + timedelta(days=210)):
        tarefa = ev["tipo"] == "tarefa"
        titulo = f"#{ev['id']} {ev['titulo']}" if tarefa else f"Ausência: {ev['titulo']}"
        linhas += [
            "BEGIN:VEVENT", f"UID:{ev['tipo']}-{ev['id']}-{f.id}@alocador", f"DTSTAMP:{agora_utc}",
            f"DTSTART:{_ics_data(ev['inicio'])}", f"DTEND:{_ics_data(ev['fim'])}",
            f"SUMMARY:{_ics_texto(titulo)}",
            f"STATUS:{'CONFIRMED' if not tarefa or ev['status'] == 'confirmada' else 'TENTATIVE'}",
        ]
        if tarefa and ev.get("local"):
            linhas.append(f"LOCATION:{_ics_texto(ev['local'])}")
        linhas.append("END:VEVENT")
    linhas.append("END:VCALENDAR")
    corpo = "\r\n".join(_ics_linha(x) for x in linhas) + "\r\n"
    return Response(corpo, media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": f'inline; filename="agenda-{f.id}.ics"'})


# ---------- tarefas ----------


def _filtros_tarefas(request: Request) -> dict:
    q = request.query_params
    return {"status": q.get("status", "ativas"), "de": _data_param(q.get("de")), "ate": _data_param(q.get("ate")),
            "tipo": q.get("tipo") or None, "contrato": (q.get("contrato") or "").strip() or None}


@router.get("/tarefas", response_class=HTMLResponse)
def lista_tarefas(request: Request, db: Session = Depends(get_db)):
    filtros = _filtros_tarefas(request)
    status = None if filtros["status"] == "todas" else filtros["status"]
    tarefas = sv.listar_tarefas(db, status, filtros["de"], filtros["ate"], filtros["tipo"], filtros["contrato"])
    return templates.TemplateResponse(
        request, "tarefas.html",
        _ctx(request, tarefas=tarefas, filtros=filtros, query=urlencode({k: v for k, v in request.query_params.items()})),
    )


@router.get("/tarefas/exportar.csv")
def exportar_tarefas(request: Request, db: Session = Depends(get_db)):
    filtros = _filtros_tarefas(request)
    status = None if filtros["status"] == "todas" else filtros["status"]
    tarefas = sv.listar_tarefas(db, status, filtros["de"], filtros["ate"], filtros["tipo"], filtros["contrato"])
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")  # ";" e BOM: o Excel em português abre direto
    w.writerow(["id", "titulo", "tipo", "local", "contrato", "inicio", "fim", "prioridade", "status",
                "equipe_confirmada", "vagas_faltando"])
    for t in tarefas:
        conf = [a for a in t.alocacoes if a.status == "confirmada"]
        faltam = sum(r.quantidade for r in t.requisitos) - len(conf)
        w.writerow([t.id, t.titulo, t.tipo, t.local or "", t.contrato or "", t.inicio.strftime("%d/%m/%Y %H:%M"),
                    t.fim.strftime("%d/%m/%Y %H:%M"), t.prioridade, STATUS_LABEL.get(t.status, t.status),
                    ", ".join(a.funcionario_nome for a in conf), max(0, faltam)])
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="tarefas-{hoje():%Y%m%d}.csv"'})


@router.get("/tarefas/nova", response_class=HTMLResponse)
def nova_tarefa(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "tarefa_form.html",
        _ctx(request, form={}, linhas=[{}], papeis=_papeis(db), contratos=sv.listar_contratos(db), t=None),
    )


@router.post("/tarefas", response_class=HTMLResponse)
async def criar_tarefa(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        t = sv.criar_tarefa(db, _tarefa_de_form(form))
    except (ValidationError, ValueError) as e:
        return templates.TemplateResponse(
            request, "tarefa_form.html",
            _ctx(request, form=_form_dict(form), linhas=_linhas_req_form(form), papeis=_papeis(db),
                 contratos=sv.listar_contratos(db), t=None, erro=_erro_validacao(e)),
            status_code=400,
        )
    sv.auditar(db, "criar_tarefa", f"#{t.id} {t.titulo} criada", AUTOR, t.id)
    db.commit()
    return _redir(f"/tarefas/{t.id}", msg="Tarefa criada. Gere a proposta de alocação.")


@router.get("/tarefas/{tarefa_id}", response_class=HTMLResponse)
def tarefa(tarefa_id: int, request: Request, db: Session = Depends(get_db)):
    t = sv.obter_tarefa(db, tarefa_id)
    ativa = t.status not in sv.ENCERRADAS
    prop = propor(db, t) if ativa else None
    vagas = {v.requisito.id: v for v in prop.vagas} if prop else {}
    contrato = prop.contrato if prop else sv.contrato_por_codigo(db, t.contrato)
    problemas = {a.id: m for a, m in sv.revalidar_confirmadas(db, t)} if ativa else {}
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
            "rejeitados": [a for a in vaga.rejeitados if a.funcionario.id not in ids] if vaga else [],
            "faltam": max(0, r.quantidade - sum(a.status == "confirmada" for a in gravadas)),
        })
    tem_proposta = any(a.status == "proposta" for a in t.alocacoes)
    horas = horas_por_dia(t.inicio, t.fim, t.horas_por_pessoa)
    return templates.TemplateResponse(
        request, "tarefa.html",
        _ctx(request, t=t, ativa=ativa, linhas=linhas, tem_proposta=tem_proposta, contrato=contrato,
             avisos_contrato=sv.avisos_contrato(t, contrato), problemas=problemas, horas=horas,
             historico=sv.listar_auditoria(db, tarefa_id=t.id, limite=30)),
    )


@router.get("/tarefas/{tarefa_id}/editar", response_class=HTMLResponse)
def editar_tarefa(tarefa_id: int, request: Request, db: Session = Depends(get_db)):
    t = sv.obter_tarefa(db, tarefa_id)
    if t.status in sv.ENCERRADAS:
        return _redir(f"/tarefas/{t.id}", erro=f"Tarefa {STATUS_LABEL[t.status].lower()} não pode ser editada")
    form = {c: getattr(t, c) for c in ("titulo", "descricao", "tipo", "local", "contrato", "prioridade")}
    form.update(inicio=t.inicio.strftime("%Y-%m-%dT%H:%M"), fim=t.fim.strftime("%Y-%m-%dT%H:%M"),
                horas_por_pessoa=f"{t.horas_por_pessoa:g}" if t.horas_por_pessoa else "")
    return templates.TemplateResponse(
        request, "tarefa_form.html",
        _ctx(request, form=form, linhas=_linhas_req_tarefa(t), papeis=_papeis(db), contratos=sv.listar_contratos(db),
             t=t, confirmados=_confirmados_por_vaga(t)),
    )


def _confirmados_por_vaga(t) -> dict:
    out: dict = {}
    for a in t.alocacoes:
        if a.status == "confirmada":
            out[a.requisito_id] = out.get(a.requisito_id, 0) + 1
    return out


@router.post("/tarefas/{tarefa_id}/editar", response_class=HTMLResponse)
async def salvar_tarefa(tarefa_id: int, request: Request, db: Session = Depends(get_db)):
    t = sv.obter_tarefa(db, tarefa_id)
    form = await request.form()
    try:
        problemas = sv.editar_tarefa(db, t, _tarefa_de_form(form), AUTOR)
    except (ValidationError, ValueError, sv.ErroNegocio) as e:
        db.rollback()
        t = sv.obter_tarefa(db, tarefa_id)
        return templates.TemplateResponse(
            request, "tarefa_form.html",
            _ctx(request, form=_form_dict(form), linhas=_linhas_req_form(form), papeis=_papeis(db),
                 contratos=sv.listar_contratos(db), t=t, confirmados=_confirmados_por_vaga(t), erro=_erro_validacao(e)),
            status_code=400,
        )
    if problemas:
        return _redir(f"/tarefas/{t.id}", erro="Tarefa salva. Quem já estava confirmado e agora não cumpre as regras: "
                                               + _problemas_txt(problemas))
    return _redir(f"/tarefas/{t.id}", msg="Tarefa salva. Propostas pendentes foram descartadas.")


@router.get("/tarefas/{tarefa_id}/os", response_class=HTMLResponse)
def ordem_servico(tarefa_id: int, request: Request, db: Session = Depends(get_db)):
    """Ordem de Serviço para imprimir (ou salvar em PDF pelo navegador) e entregar à equipe de campo."""
    t = sv.obter_tarefa(db, tarefa_id)
    equipe = [a for a in t.alocacoes if a.status == "confirmada"]
    papeis = {r.id: r.papel for r in t.requisitos}
    return templates.TemplateResponse(
        request, "os.html",
        _ctx(request, t=t, equipe=equipe, papeis=papeis, contrato=sv.contrato_por_codigo(db, t.contrato),
             emitida=agora()),
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
    motivo = _txt(form.get("motivo"))
    if forcar and not motivo:
        return _redir(f"/tarefas/{tarefa_id}", erro="Para forçar uma alocação, informe o motivo (fica na auditoria).")
    if form.get("funcionario_id"):
        escolhas = [schemas.Escolha(requisito_id=int(form["requisito_id"]), funcionario_id=int(form["funcionario_id"]))]
    return _acao_tarefa(tarefa_id, db, lambda t: sv.confirmar(db, t, escolhas, forcar, motivo, AUTOR),
                        "Alocação confirmada")


@router.post("/tarefas/{tarefa_id}/concluir")
def web_concluir(tarefa_id: int, db: Session = Depends(get_db)):
    return _acao_tarefa(tarefa_id, db, lambda t: sv.encerrar_tarefa(db, t, "concluida", AUTOR), "Tarefa concluída")


@router.post("/tarefas/{tarefa_id}/cancelar")
def web_cancelar(tarefa_id: int, db: Session = Depends(get_db)):
    return _acao_tarefa(tarefa_id, db, lambda t: sv.encerrar_tarefa(db, t, "cancelada", AUTOR), "Tarefa cancelada")


@router.post("/alocacoes/{alocacao_id}/cancelar")
def web_cancelar_alocacao(alocacao_id: int, db: Session = Depends(get_db)):
    a = sv.obter_alocacao(db, alocacao_id)
    try:
        sv.cancelar_alocacao(db, a, AUTOR)
    except sv.ErroNegocio as e:
        return _redir(f"/tarefas/{a.tarefa_id}", erro=e.mensagem)
    return _redir(f"/tarefas/{a.tarefa_id}", msg=f"Alocação de {a.funcionario_nome} cancelada")


# ---------- semana ----------


@router.get("/semana", response_class=HTMLResponse)
def semana(request: Request, inicio: str | None = None, papel: str | None = None, db: Session = Depends(get_db)):
    """Funcionários × dias da semana: tarefas, ausências e horas usadas da carga semanal."""
    seg = segunda_da_semana(_data_param(inicio) or hoje())
    grade = sv.grade_semana(db, seg, papel or None)
    return templates.TemplateResponse(
        request, "semana.html",
        _ctx(request, grade=grade, segunda=seg, anterior=seg - timedelta(days=7), proxima=seg + timedelta(days=7),
             papel=papel or "", papeis=_papeis(db), hoje=hoje()),
    )


# ---------- contratos ----------


def _contrato_de_form(form) -> schemas.ContratoIn:
    return schemas.ContratoIn(
        codigo=(form.get("codigo") or "").strip(),
        cliente=_txt(form.get("cliente")),
        certificacoes=_itens(form.get("certificacoes")),
        anos_experiencia_min=_num(form.get("anos_experiencia_min"), 0),
        exige_tecnico_seguranca=form.get("exige_tecnico_seguranca") == "on",
        observacoes=_txt(form.get("observacoes")),
        texto=_txt(form.get("texto")),
    )


def _valores_contrato(c) -> dict:
    return {"codigo": c.codigo, "cliente": c.cliente or "", "certificacoes": ", ".join(c.certificacoes or []),
            "anos_experiencia_min": f"{c.anos_experiencia_min:g}", "exige_tecnico_seguranca": c.exige_tecnico_seguranca,
            "observacoes": c.observacoes or "", "texto": c.texto or ""}


@router.get("/contratos", response_class=HTMLResponse)
def lista_contratos(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "contratos.html", _ctx(request, contratos=sv.listar_contratos(db), form={}, c=None,
                                        ia=provedores.configurado()),
    )


@router.post("/contratos", response_class=HTMLResponse)
async def criar_contrato(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        c = sv.salvar_contrato(db, _contrato_de_form(form), autor=AUTOR)
    except (ValidationError, ValueError, sv.ErroNegocio) as e:
        db.rollback()
        valores = dict(form)
        valores["exige_tecnico_seguranca"] = form.get("exige_tecnico_seguranca") == "on"
        return templates.TemplateResponse(
            request, "contratos.html",
            _ctx(request, contratos=sv.listar_contratos(db), form=valores, c=None, erro=_erro_validacao(e),
                 ia=provedores.configurado()),
            status_code=400,
        )
    return _redir(f"/contratos/{c.id}", msg=f"Contrato {c.codigo} cadastrado")


@router.get("/contratos/{contrato_id}", response_class=HTMLResponse)
def contrato(contrato_id: int, request: Request, db: Session = Depends(get_db)):
    c = sv.obter_contrato(db, contrato_id)
    tarefas = sv.listar_tarefas(db, contrato=c.codigo)
    return templates.TemplateResponse(
        request, "contratos.html",
        _ctx(request, contratos=sv.listar_contratos(db), form=_valores_contrato(c), c=c, tarefas=tarefas,
             ia=provedores.configurado()),
    )


@router.post("/contratos/{contrato_id}", response_class=HTMLResponse)
async def salvar_contrato(contrato_id: int, request: Request, db: Session = Depends(get_db)):
    c = sv.obter_contrato(db, contrato_id)
    form = await request.form()
    try:
        sv.salvar_contrato(db, _contrato_de_form(form), c, AUTOR)
    except (ValidationError, ValueError, sv.ErroNegocio) as e:
        db.rollback()
        return _redir(f"/contratos/{c.id}", erro=_erro_validacao(e))
    return _redir(f"/contratos/{c.id}", msg="Contrato salvo. As tarefas com este código já seguem as regras novas.")


@router.post("/contratos/{contrato_id}/extrair")
def extrair_contrato(contrato_id: int, db: Session = Depends(get_db)):
    """Lê o texto com o modelo de IA e soma as exigências encontradas às que já estão cadastradas."""
    c = sv.obter_contrato(db, contrato_id)
    if not c.texto:
        return _redir(f"/contratos/{c.id}", erro="Cole o texto do contrato antes de extrair as exigências.")
    try:
        regras = contratos.extrair_regras(c.texto)
    except Exception as e:  # sem chave, cota, rede...
        return _redir(f"/contratos/{c.id}", erro=str(e) if isinstance(e, RuntimeError) else provedores.explicar_erro(e))
    dados = schemas.ContratoIn(
        codigo=c.codigo, cliente=c.cliente or regras.cliente,
        certificacoes=list(c.certificacoes or []) + regras.certificacoes,
        anos_experiencia_min=max(c.anos_experiencia_min or 0, regras.anos_experiencia_min or 0),
        exige_tecnico_seguranca=c.exige_tecnico_seguranca or regras.exige_tecnico_seguranca,
        observacoes="\n".join(x for x in (c.observacoes, regras.observacoes) if x) or None, texto=c.texto,
    )
    sv.salvar_contrato(db, dados, c, "painel (extração com IA)")
    return _redir(f"/contratos/{c.id}", msg="Exigências extraídas do texto. Revise antes de usar.")


@router.post("/contratos/{contrato_id}/remover")
def remover_contrato(contrato_id: int, db: Session = Depends(get_db)):
    c = sv.obter_contrato(db, contrato_id)
    codigo = c.codigo
    sv.remover_contrato(db, c, AUTOR)
    return _redir("/contratos", msg=f"Contrato {codigo} removido. Tarefas com esse código deixam de ter regras extras.")


# ---------- auditoria ----------


@router.get("/auditoria", response_class=HTMLResponse)
def auditoria(request: Request, acao: str | None = None, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "auditoria.html", _ctx(request, registros=sv.listar_auditoria(db, acao or None), acao=acao or ""),
    )
