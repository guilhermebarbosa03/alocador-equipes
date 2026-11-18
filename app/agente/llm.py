"""Chamada ao modelo: uma por mensagem do usuário.

O modelo recebe o contexto atual (equipe, tarefas, data de hoje) e escolhe UMA intenção.
Vai para o modelo: nome, função, especialidades e certificações (com validade). Não vão: e-mail,
Telegram, resumo livre. No modo equipe, o funcionário só vê os próprios dados.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import provedores
from .. import services as sv
from ..models import Funcionario
from .intencoes import FERRAMENTAS

# (mensagens) -> (nome da intenção, argumentos)
Interpretador = Callable[[list[BaseMessage]], tuple[str, dict]]

DIAS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
MAX_TAREFAS_CONTEXTO = 40

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
  Certificação marcada como VENCIDA não vale: não diga que a pessoa tem.
- Mensagem de voz: ouça o áudio e trate como se fosse texto.
- Português, curto e direto.

Hoje: {hoje}
{remetente}
Funções cadastradas: {papeis}
Contratos cadastrados: {contratos}

Funcionários ativos (id · nome · função · especialidades nível 1-5 · certificações e validade):
{funcionarios}

Tarefas em andamento, das mais próximas para as mais distantes (id · título · período · prioridade · status · equipe confirmada):
{tarefas}
"""

PROMPT_EQUIPE = """Você é o assistente da empresa para a equipe de campo. Quem fala é um funcionário, pelo Telegram.

Escolha UMA ferramenta para o pedido e preencha os argumentos.
- Ausência ("estou de atestado hoje", "não vou amanhã"): use InformarAusencia. Ela vai para o gestor aprovar.
- Datas em AAAA-MM-DD, a partir de hoje. Não preencha horas que a pessoa não disse.
- Você só conhece os dados desta pessoa. Não fale de outros funcionários além da equipe das tarefas dela.
- Mensagem de voz: ouça o áudio e trate como se fosse texto.
- Português, curto e gentil.

Hoje: {hoje}
Quem está falando: {nome} ({papel})

Próximas tarefas dessa pessoa (id · título · período · local):
{tarefas}
"""


def _certificacoes(f: Funcionario, agora: datetime) -> str:
    hoje, partes = agora.date(), []
    melhores: dict[str, object] = {}
    for c in f.certificacoes:  # repetida (vencida e renovada): vale a de validade mais longa
        atual = melhores.get(c.nome.lower())
        if atual is None or (atual.validade is not None and (c.validade is None or c.validade > atual.validade)):
            melhores[c.nome.lower()] = c
    for c in melhores.values():
        if c.validade is None:
            partes.append(c.nome)
        elif c.validade < hoje:
            partes.append(f"{c.nome} (VENCIDA em {c.validade:%d/%m/%Y})")
        else:
            partes.append(f"{c.nome} (até {c.validade:%d/%m/%Y})")
    return ", ".join(partes) or "-"


def montar_contexto(db: Session, agora: datetime, remetente_id: int | None = None) -> str:
    funcs = sv.listar_funcionarios(db)
    linhas_f = []
    for f in funcs:
        esp = ", ".join(f"{e.nome} {e.nivel}" for e in f.especialidades) or "-"
        extra = "" if f.disponivel_viagem else " · não viaja"
        linhas_f.append(f"- {f.id} · {f.nome} · {f.papel} · {esp} · {_certificacoes(f, agora)}{extra}")

    limite = agora - timedelta(days=2)
    # das mais próximas para as mais distantes: se cortar, cortam as de longe, não as de amanhã
    tarefas = [t for t in sv.listar_tarefas(db, status="ativas", recentes_primeiro=False) if t.fim >= limite]
    linhas_t = []
    for t in tarefas[:MAX_TAREFAS_CONTEXTO]:
        equipe = ", ".join(a.funcionario_nome for a in t.alocacoes if a.status == "confirmada") or "ninguém"
        linhas_t.append(
            f"- #{t.id} · {t.titulo} · {t.inicio:%d/%m %H:%M} a {t.fim:%d/%m %H:%M} · p{t.prioridade} · {t.status} · {equipe}"
        )
    if len(tarefas) > MAX_TAREFAS_CONTEXTO:
        linhas_t.append(f"(+{len(tarefas) - MAX_TAREFAS_CONTEXTO} tarefas mais distantes não listadas; use o id se o gestor disser)")

    remetente = ""
    if remetente_id and (f := db.get(Funcionario, remetente_id)):
        remetente = f"Quem está falando: {f.nome} (id {f.id}). \"Eu\" se refere a essa pessoa."

    return PROMPT.format(
        hoje=f"{DIAS[agora.weekday()]}, {agora:%Y-%m-%d %H:%M}",
        remetente=remetente,
        papeis=", ".join(sorted({f.papel for f in funcs})) or "nenhuma",
        contratos=", ".join(c.codigo for c in sv.listar_contratos(db)) or "nenhum",
        funcionarios="\n".join(linhas_f) or "(nenhum)",
        tarefas="\n".join(linhas_t) or "(nenhuma)",
    )


def montar_contexto_equipe(db: Session, agora: datetime, f: Funcionario) -> str:
    inicio = agora - timedelta(days=1)
    eventos = [e for e in sv.agenda(db, f, inicio, agora + timedelta(days=30)) if e["tipo"] == "tarefa"]
    linhas = [
        f"- #{e['id']} · {e['titulo']} · {e['inicio']:%d/%m %H:%M} a {e['fim']:%d/%m %H:%M} · {e.get('local') or '-'}"
        for e in eventos if e["status"] == "confirmada"
    ]
    return PROMPT_EQUIPE.format(
        hoje=f"{DIAS[agora.weekday()]}, {agora:%Y-%m-%d %H:%M}", nome=f.nome, papel=f.papel,
        tarefas="\n".join(linhas) or "(nenhuma)",
    )


def funcionario_por_telegram(db: Session, telegram_id: int | str) -> int | None:
    return db.scalar(
        select(Funcionario.id).where(Funcionario.telegram_id == str(telegram_id), Funcionario.ativo.is_(True))
    )


def historico(mensagens: list[BaseMessage], n: int = 8) -> list[BaseMessage]:
    """Só texto de usuário e do assistente, últimas n mensagens, começando por uma do usuário
    (alguns provedores recusam conversa que começa pelo assistente)."""
    recentes = [m for m in mensagens if isinstance(m, (HumanMessage, AIMessage)) and m.content][-n:]
    while recentes and not isinstance(recentes[0], HumanMessage):
        recentes.pop(0)
    return recentes


def com_audio(mensagens: list[BaseMessage], audio: dict | None) -> list[BaseMessage]:
    """Troca a última mensagem do usuário pelo áudio. Só chega aqui com provedor que ouve áudio (Gemini):
    o bot recusa a mensagem de voz antes, se o modelo escolhido não aceitar."""
    if not audio:
        return mensagens
    for i in range(len(mensagens) - 1, -1, -1):
        if isinstance(mensagens[i], HumanMessage):
            conteudo = [
                {"type": "text", "text": "Mensagem de voz do usuário:"},
                {"type": "audio", "base64": audio["base64"], "mime_type": audio.get("mime_type") or "audio/ogg"},
            ]
            return [*mensagens[:i], HumanMessage(conteudo), *mensagens[i + 1:]]
    return mensagens


def criar_interpretador(ferramentas: list = FERRAMENTAS) -> Interpretador:
    """Liga o modelo escolhido em LLM_PROVIDER às intenções. tool_choice="any": sempre uma ferramenta."""
    llm = provedores.criar_modelo(timeout=60)
    com_ferramentas = llm.bind_tools(ferramentas, tool_choice="any")
    resposta_texto = ferramentas[-1].__name__  # a última ferramenta é sempre a de responder em texto

    def interpretar(mensagens: list[BaseMessage]) -> tuple[str, dict]:
        resposta = com_ferramentas.invoke(mensagens)
        if not resposta.tool_calls:
            return resposta_texto, {"texto": resposta.text or "Não entendi. Pode reformular?"}
        chamada = resposta.tool_calls[0]
        return chamada["name"], chamada["args"]

    return interpretar


def mensagens_para_modelo(contexto: str, mensagens: list[BaseMessage], audio: dict | None = None) -> list[BaseMessage]:
    return com_audio([SystemMessage(contexto), *historico(mensagens)], audio)
