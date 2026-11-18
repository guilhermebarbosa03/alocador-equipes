"""Qual modelo de IA o app usa: Gemini (padrão), Claude, OpenAI ou Ollama (local).

A escolha fica em LLM_PROVIDER no .env. Cada provedor tem a própria chave e o próprio modelo
(GEMINI_API_KEY/GEMINI_MODEL, ANTHROPIC_API_KEY/ANTHROPIC_MODEL...), então dá para deixar várias
chaves no .env e trocar só uma linha.

Todos são chat models do LangChain, com a mesma interface (invoke, bind_tools, with_structured_output).
Por isso o agente e a extração de contratos não sabem qual está em uso. Para outro provedor que o
LangChain suporta (Mistral, Bedrock...), basta uma função que crie o modelo e uma entrada em PROVEDORES.
APIs compatíveis com a da OpenAI (OpenRouter, Groq, DeepSeek, LM Studio...) já funcionam com
LLM_PROVIDER=openai e OPENAI_BASE_URL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from . import config

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

OPENAI_URL_PADRAO = "https://api.openai.com/v1"


def _gemini(modelo: str, chave: str | None, timeout: float) -> BaseChatModel:
    from langchain_google_genai import ChatGoogleGenerativeAI

    # o conversor avisa "Key '$defs' is not supported" mesmo resolvendo as referências corretamente
    logging.getLogger("langchain_google_genai").setLevel(logging.ERROR)
    return ChatGoogleGenerativeAI(model=modelo, api_key=chave, max_retries=2, timeout=timeout)


def _claude(modelo: str, chave: str | None, timeout: float) -> BaseChatModel:
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(model=modelo, api_key=chave, max_retries=2, timeout=timeout)


def _openai(modelo: str, chave: str | None, timeout: float) -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    # URL sempre explícita: OPENAI_BASE_URL vazia no ambiente (docker compose) quebraria o cliente
    return ChatOpenAI(model=modelo, api_key=chave, base_url=config.OPENAI_BASE_URL or OPENAI_URL_PADRAO,
                      max_retries=2, timeout=timeout)


def _ollama(modelo: str, chave: str | None, timeout: float) -> BaseChatModel:
    from langchain_ollama import ChatOllama

    return ChatOllama(model=modelo, base_url=config.OLLAMA_BASE_URL, client_kwargs={"timeout": timeout})


@dataclass(frozen=True)
class Provedor:
    rotulo: str  # nome nas mensagens: "Gemini", "Claude"...
    pacote: str  # pacote pip da integração do LangChain
    var_chave: str | None  # variável do .env com a chave (None = não precisa)
    var_modelo: str
    onde_criar_chave: str
    aceita_audio: bool  # ouve as mensagens de voz do Telegram na mesma chamada
    construir: Callable[[str, str | None, float], BaseChatModel]

    @property
    def chave(self) -> str | None:
        return getattr(config, self.var_chave) if self.var_chave else None

    @property
    def modelo(self) -> str:
        return getattr(config, self.var_modelo)

    @property
    def configurado(self) -> bool:
        return self.var_chave is None or bool(self.chave)


PROVEDORES: dict[str, Provedor] = {
    "gemini": Provedor("Gemini", "langchain-google-genai", "GEMINI_API_KEY", "GEMINI_MODEL",
                       "https://aistudio.google.com/apikey", True, _gemini),
    "anthropic": Provedor("Claude", "langchain-anthropic", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
                          "https://console.anthropic.com/settings/keys", False, _claude),
    "openai": Provedor("OpenAI", "langchain-openai", "OPENAI_API_KEY", "OPENAI_MODEL",
                       "https://platform.openai.com/api-keys", False, _openai),
    "ollama": Provedor("Ollama", "langchain-ollama", None, "OLLAMA_MODEL",
                       "https://ollama.com/download", False, _ollama),
}
APELIDOS = {"google": "gemini", "claude": "anthropic", "gpt": "openai", "chatgpt": "openai"}


def atual() -> Provedor:
    nome = APELIDOS.get(config.LLM_PROVIDER, config.LLM_PROVIDER)
    if nome not in PROVEDORES:
        raise RuntimeError(
            f"LLM_PROVIDER='{config.LLM_PROVIDER}' não existe. Use um destes no .env: {', '.join(PROVEDORES)}."
        )
    return PROVEDORES[nome]


def configurado() -> bool:
    """Há chave para o provedor escolhido (o painel usa para liberar o botão de IA)."""
    try:
        return atual().configurado
    except RuntimeError:
        return False


def aceita_audio() -> bool:
    try:
        return atual().aceita_audio
    except RuntimeError:
        return False


def descricao() -> str:
    """Para logs: "Gemini (gemini-3.5-flash-lite)"."""
    try:
        p = atual()
    except RuntimeError as e:
        return str(e)
    return f"{p.rotulo} ({p.modelo})"


def criar_modelo(timeout: float = 60) -> BaseChatModel:
    """O chat model do provedor escolhido. Erros de configuração viram RuntimeError com o que fazer."""
    p = atual()
    if not p.configurado:
        raise RuntimeError(
            f"Defina {p.var_chave} no arquivo .env (LLM_PROVIDER={config.LLM_PROVIDER}). "
            f"Crie uma chave em {p.onde_criar_chave}"
        )
    try:
        return p.construir(p.modelo, p.chave, timeout)
    except ImportError as e:
        raise RuntimeError(f"Falta a integração do {p.rotulo}. Instale com: pip install {p.pacote}") from e


def explicar_erro(err: Exception) -> str:
    """Mensagem curta para o usuário quando a chamada ao modelo falha (sem mostrar a chave)."""
    try:
        p = atual()
    except RuntimeError as e:
        return str(e)
    texto = str(err)
    baixo = texto.lower()
    local = p.var_chave is None

    if p.rotulo == "Gemini" and ("402" in texto or "prepayment" in baixo):
        return (
            "A chave do Gemini é de um projeto com faturamento pré-pago e sem créditos. "
            "Crie uma chave num projeto SEM faturamento (plano gratuito) no AI Studio e troque GEMINI_API_KEY no .env."
        )
    if any(k in baixo for k in ("402", "credit balance", "insufficient_quota")):
        return f"A conta do {p.rotulo} está sem créditos. Confira o faturamento ou troque {p.var_chave} no .env."
    if local and any(k in baixo for k in ("connect", "refused", "unreachable")):
        return f"Não consegui falar com o Ollama em {config.OLLAMA_BASE_URL}. Ele está aberto?"
    if any(k in texto for k in ("429", "RESOURCE_EXHAUSTED")) or any(k in baixo for k in ("quota", "rate limit", "rate_limit")):
        limite = "limite gratuito" if p.rotulo == "Gemini" else "limite de uso"
        return f"Atingi o {limite} do {p.rotulo}. Tente de novo em alguns minutos (ou use o painel)."
    if any(k in texto for k in ("529", "503", "UNAVAILABLE")) or "overloaded" in baixo:
        return f"O {p.rotulo} está sobrecarregado agora. Tente de novo em instantes."
    if not local and (
        any(k in texto for k in ("API_KEY_INVALID", "API key not valid", "PERMISSION_DENIED", "401", "403"))
        or any(k in baixo for k in ("authentication", "invalid x-api-key", "incorrect api key", "invalid api key"))
    ):
        return f"A chave do {p.rotulo} foi recusada. Confira {p.var_chave} no .env."
    if "404" in texto or "not found" in baixo or "not_found" in baixo:
        dica = f" Baixe com: ollama pull {p.modelo}" if local else ""
        return f"O modelo '{p.modelo}' não foi encontrado no {p.rotulo}. Ajuste {p.var_modelo} no .env.{dica}"
    return "Não consegui falar com o modelo de IA agora. Tente de novo em instantes."
