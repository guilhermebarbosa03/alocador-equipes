"""Chamada ao modelo: uma por mensagem do usuário.

O modelo recebe o contexto atual (equipe, tarefas, data de hoje) e escolhe UMA intenção.
Vai para o modelo: nome, função, especialidades e certificações. Não vão: e-mail, Telegram, resumo livre.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from .. import services as sv
from ..models import Funcionario
from .intencoes import FERRAMENTAS

# (mensagens) -> (nome da intenção, argumentos)
Interpretador = Callable[[list[BaseMessage]], tuple[str, dict]]

DIAS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]

PROMPT = """Você é o assistente de alocação de equipe de uma empresa. Gestores mandam mensagens curtas pelo Telegram.

Seu trabalho é escolher UMA ferramenta que representa o pedido e preencher os argumentos.
Você NÃO escolhe quem será alocado: o sistema faz isso com regras e depois pede aprovação ao gestor.

Regras:
- Use os ids das listas abaixo. Se um nome não existir ou for ambíguo, use Responder para perguntar.
- Datas em AAAA-MM-DD, calculadas a partir de hoje ("amanhã", "sexta", "semana que vem").
- Não preencha horas que o usuário não disse (o sistema usa 08:00-17:00). Quantidade não dita = 1.
- Se faltar algo essencial (quando? qual função?), use Responder com UMA pergunta curta.
- "papel" deve ser exatamente uma das funções cadastradas.
- Especialidades e certificações: use os nomes como aparecem no cadastro.
- Serviço em cliente, obra, viagem ou fora da empresa = tipo "campo".
- Perguntas sobre a equipe ("quem tem NR35?"): use Responder, só com os dados abaixo. Nunca invente.
- Português, curto e direto.

Hoje: {hoje}
{remetente}
Funções cadastradas: {papeis}

Funcionários ativos (id · nome · função · especialidades nível 1-5 · certificações):
{funcionarios}

Tarefas em andamento (id · título · período · status · equipe confirmada):
{tarefas}
"""


def montar_contexto(db: Session, agora: datetime, remetente_id: int | None = None) -> str:
    funcs = sv.listar_funcionarios(db)
    linhas_f = []
    for f in funcs:
        esp = ", ".join(f"{e.nome} {e.nivel}" for e in f.especialidades) or "-"
        cert = ", ".join(c.nome for c in f.certificacoes) or "-"
        extra = "" if f.disponivel_viagem else " · não viaja"
        linhas_f.append(f"- {f.id} · {f.nome} · {f.papel} · {esp} · {cert}{extra}")

    limite = agora - timedelta(days=2)
    linhas_t = []
    for t in sv.listar_tarefas(db):
        if t.status in sv.ENCERRADAS or t.fim < limite:
            continue
        equipe = ", ".join(a.funcionario_nome for a in t.alocacoes if a.status == "confirmada") or "ninguém"
        linhas_t.append(
            f"- #{t.id} · {t.titulo} · {t.inicio:%d/%m %H:%M} a {t.fim:%d/%m %H:%M} · {t.status} · {equipe}"
        )

    remetente = ""
    if remetente_id and (f := db.get(Funcionario, remetente_id)):
        remetente = f"Quem está falando: {f.nome} (id {f.id}). \"Eu\" se refere a essa pessoa."

    return PROMPT.format(
        hoje=f"{DIAS[agora.weekday()]}, {agora:%Y-%m-%d %H:%M}",
        remetente=remetente,
        papeis=", ".join(sorted({f.papel for f in funcs})) or "nenhuma",
        funcionarios="\n".join(linhas_f) or "(nenhum)",
        tarefas="\n".join(linhas_t[:40]) or "(nenhuma)",
    )


def funcionario_por_telegram(db: Session, telegram_id: int | str) -> int | None:
    return db.scalar(
        select(Funcionario.id).where(Funcionario.telegram_id == str(telegram_id), Funcionario.ativo.is_(True))
    )


def historico(mensagens: list[BaseMessage], n: int = 8) -> list[BaseMessage]:
    """Só texto de usuário e do assistente, últimas n mensagens."""
    return [m for m in mensagens if isinstance(m, (HumanMessage, AIMessage)) and m.content][-n:]


def criar_interpretador() -> Interpretador:
    if not config.GEMINI_API_KEY:
        raise RuntimeError(
            "Defina GEMINI_API_KEY no arquivo .env. Crie uma chave gratuita em https://aistudio.google.com/apikey"
        )
    import logging

    from langchain_google_genai import ChatGoogleGenerativeAI

    # o conversor avisa "Key '$defs' is not supported" mesmo resolvendo as referências corretamente
    logging.getLogger("langchain_google_genai").setLevel(logging.ERROR)

    llm = ChatGoogleGenerativeAI(
        model=config.GEMINI_MODEL, api_key=config.GEMINI_API_KEY, max_retries=2, timeout=60
    )
    com_ferramentas = llm.bind_tools(FERRAMENTAS, tool_choice="any")

    def interpretar(mensagens: list[BaseMessage]) -> tuple[str, dict]:
        resposta = com_ferramentas.invoke(mensagens)
        if not resposta.tool_calls:
            return "Responder", {"texto": resposta.text or "Não entendi. Pode reformular?"}
        chamada = resposta.tool_calls[0]
        return chamada["name"], chamada["args"]

    return interpretar


def mensagens_para_modelo(contexto: str, mensagens: list[BaseMessage]) -> list[BaseMessage]:
    return [SystemMessage(contexto), *historico(mensagens)]
