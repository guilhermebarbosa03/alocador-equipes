import secrets
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import text

from . import api, config, seguranca, web
from .db import engine, init_db
from .services import ErroNegocio


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Alocador",
    description=(
        "Cadastro de equipe, disponibilidade e alocação de pessoas em tarefas e idas a campo.\n\n"
        "Com `API_TOKEN` definido, mande `Authorization: Bearer <token>` (botão **Authorize**)."
    ),
    version="0.2.0",
    lifespan=lifespan,
)
app.include_router(api.router, prefix="/api")
app.include_router(web.router)


def _publico(path: str) -> bool:
    # .ics: apps de calendário não fazem login; o link carrega um token próprio, conferido na rota
    return path in ("/login", "/saude") or path.endswith(".ics")


@app.middleware("http")
async def seguranca_web(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api"):
        return await call_next(request)  # a API confere o próprio token (api.exigir_token)

    if config.PAINEL_SENHA and not _publico(path) and not seguranca.verificar(request.cookies.get("sessao")):
        if request.method in ("GET", "HEAD"):
            destino = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?proximo={quote(destino)}", status_code=303)
        return HTMLResponse("<p>Sessão expirada. <a href='/login'>Entrar</a></p>", status_code=401)

    cookie_csrf = request.cookies.get("csrf")
    request.state.csrf = cookie_csrf if cookie_csrf and len(cookie_csrf) >= 20 else secrets.token_urlsafe(24)
    request.state.flash = seguranca.verificar(request.cookies.get("flash")) or {}
    request.state.login_ativo = bool(config.PAINEL_SENHA)

    response = await call_next(request)

    https = request.url.scheme == "https"
    if cookie_csrf != request.state.csrf:
        response.set_cookie("csrf", request.state.csrf, max_age=30 * 86400, httponly=True, samesite="lax", secure=https)
    if request.cookies.get("flash") and not any(
        h.startswith("flash=") for h in response.headers.getlist("set-cookie")
    ):
        response.delete_cookie("flash")  # aviso já mostrado: F5 não repete
    return response


@app.exception_handler(ErroNegocio)
async def _erro_negocio(request: Request, exc: ErroNegocio):
    if request.url.path.startswith("/api"):
        corpo = {"detail": exc.mensagem}
        if exc.problemas:
            corpo["problemas"] = exc.problemas
        return JSONResponse(corpo, status_code=exc.status)
    if not hasattr(request.state, "csrf"):
        request.state.csrf, request.state.flash, request.state.login_ativo = "", {}, bool(config.PAINEL_SENHA)
    return web.templates.TemplateResponse(
        request, "erro.html", {"request": request, "erro_pagina": exc.mensagem, "problemas": exc.problemas},
        status_code=exc.status,
    )


@app.get("/saude", include_in_schema=False)
def saude():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"ok": False, "banco": "indisponível"}, status_code=503)
    return {"ok": True}
