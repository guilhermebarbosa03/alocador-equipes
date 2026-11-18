"""Contratos: busca de cláusulas e extração de exigências.

A busca é por palavras (BM25 simplificado), sem embeddings: roda em SQLite ou Postgres, sem modelo
local e sem chamar a API. Serve para o agente responder "o que o contrato X diz sobre EPI?" citando
o trecho. A extração usa o Gemini uma vez, no cadastro, e o gestor revisa antes de salvar.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from pydantic import BaseModel, Field

from .texto import norm

PARADAS = set(
    "a o e de da do das dos em no na nos nas um uma uns umas para por com sem que se ao aos as os ou "
    "qual quais quanto como onde quando sobre ser deve devem sera serao pelo pela pelos pelas contrato "
    "diz fala exige exigem tem ha isso este esta esse essa".split()
)
_INICIO_CLAUSULA = re.compile(r"^\s*(cl[aá]usula\b|\d+(\.\d+)*[.)\-–]\s|[ivxlc]+[.)\-–]\s|§)", re.IGNORECASE)


def _termos(texto: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", norm(texto)) if t not in PARADAS and len(t) > 1]


def clausulas(texto: str | None, tamanho_max: int = 900) -> list[str]:
    """Quebra o texto em cláusulas: por linhas em branco e por linhas que começam como cláusula."""
    if not texto:
        return []
    blocos, atual = [], []
    for linha in texto.splitlines():
        if not linha.strip() or (_INICIO_CLAUSULA.match(linha) and atual):
            if atual:
                blocos.append(" ".join(atual).strip())
            atual = [linha.strip()] if linha.strip() else []
        else:
            atual.append(linha.strip())
    if atual:
        blocos.append(" ".join(atual).strip())
    out = []
    for b in blocos:
        while len(b) > tamanho_max:
            corte = b.rfind(". ", 0, tamanho_max)
            corte = corte + 1 if corte > tamanho_max // 3 else tamanho_max
            out.append(b[:corte].strip())
            b = b[corte:].strip()
        if b:
            out.append(b)
    return out


def buscar_clausulas(texto: str | None, pergunta: str, k: int = 3) -> list[str]:
    """Trechos do contrato mais ligados à pergunta, na ordem em que aparecem no contrato."""
    partes = clausulas(texto)
    consulta = set(_termos(pergunta))
    if not partes or not consulta:
        return []
    docs = [Counter(_termos(p)) for p in partes]
    media = sum(sum(d.values()) for d in docs) / len(docs) or 1
    n = len(docs)
    df = Counter(t for d in docs for t in set(d))
    notas = []
    for i, d in enumerate(docs):
        tam = sum(d.values()) or 1
        nota = 0.0
        for t in consulta:
            if d[t]:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                nota += idf * d[t] * 2.2 / (d[t] + 1.2 * (0.25 + 0.75 * tam / media))
        if nota > 0:
            notas.append((nota, i))
    melhores = sorted(notas, reverse=True)[:k]
    return [partes[i] for _, i in sorted(melhores, key=lambda x: x[1])]


class RegrasContrato(BaseModel):
    """Exigências obrigatórias para a equipe que executa serviços deste contrato."""

    cliente: str | None = Field(None, description="Nome do cliente/contratante, se aparecer")
    certificacoes: list[str] = Field(
        default_factory=list, description='Certificações/NRs obrigatórias para quem executa, ex.: ["NR10", "NR35"]'
    )
    anos_experiencia_min: float = Field(0, description="Experiência mínima exigida da equipe, em anos (0 se não disser)")
    exige_tecnico_seguranca: bool = Field(False, description="Se exige técnico de segurança do trabalho no local")
    observacoes: str | None = Field(None, description="Outras exigências relevantes para a equipe, em uma ou duas frases")


def extrair_regras(texto: str, modelo=None) -> RegrasContrato:
    """Lê o contrato com o Gemini e devolve as exigências estruturadas. `modelo` permite testar sem a API."""
    if modelo is None:
        from . import config

        if not config.GEMINI_API_KEY:
            raise RuntimeError("Defina GEMINI_API_KEY no .env para extrair as regras automaticamente.")
        from langchain_google_genai import ChatGoogleGenerativeAI

        modelo = ChatGoogleGenerativeAI(
            model=config.GEMINI_MODEL, api_key=config.GEMINI_API_KEY, max_retries=2, timeout=90
        ).with_structured_output(RegrasContrato)
    prompt = (
        "Extraia do contrato abaixo SOMENTE exigências obrigatórias para a equipe que executa o serviço. "
        "Não invente: se o texto não disser, deixe vazio.\n\n" + texto[:60000]
    )
    resultado = modelo.invoke(prompt)
    return resultado if isinstance(resultado, RegrasContrato) else RegrasContrato.model_validate(resultado)
