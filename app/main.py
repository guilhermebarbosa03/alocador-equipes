import html
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import api, web
from .db import init_db
from .services import ErroNegocio


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Alocador",
    description="Cadastro de equipe, disponibilidade e alocação de pessoas em tarefas e idas a campo.",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(api.router, prefix="/api")
app.include_router(web.router)


@app.exception_handler(ErroNegocio)
async def _erro_negocio(request: Request, exc: ErroNegocio):
    if request.url.path.startswith("/api"):
        corpo = {"detail": exc.mensagem}
        if exc.problemas:
            corpo["problemas"] = exc.problemas
        return JSONResponse(corpo, status_code=exc.status)
    return HTMLResponse(
        f"<p>{html.escape(exc.mensagem)}</p><p><a href='/'>Voltar ao painel</a></p>", status_code=exc.status
    )


@app.get("/saude", include_in_schema=False)
def saude():
    return {"ok": True}
