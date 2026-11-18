"""Intenções que o modelo pode escolher. Cada classe vira uma "ferramenta" para o modelo.

O modelo só escolhe a intenção e preenche os campos; quem executa é código determinístico.
Campos são simples (texto, número) porque modelos pequenos erram menos assim.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Literal

from pydantic import BaseModel, Field

HORA_INICIO_PADRAO = time(8, 0)
HORA_FIM_PADRAO = time(17, 0)


class _Periodo(BaseModel):
    data_inicio: str = Field(description="Data de início no formato AAAA-MM-DD")
    hora_inicio: str | None = Field(None, description="Hora de início HH:MM, se o usuário disse")
    data_fim: str | None = Field(None, description="Data de fim AAAA-MM-DD, se for diferente do início")
    hora_fim: str | None = Field(None, description="Hora de fim HH:MM, se o usuário disse")


class Vaga(BaseModel):
    papel: str = Field(description="Função, escrita exatamente como na lista de papéis cadastrados")
    quantidade: int = Field(1, ge=1, description="Quantas pessoas dessa função")
    especialidades: list[str] = Field(
        default_factory=list,
        description='Especialidades exigidas, como no cadastro. Nível mínimo opcional após dois-pontos: ["Elétrica:3"]',
    )
    certificacoes: list[str] = Field(default_factory=list, description='Certificações exigidas: ["NR10", "NR35"]')
    desejaveis: list[str] = Field(default_factory=list, description="Especialidades que ajudam mas não são obrigatórias")
    anos_experiencia_min: float = Field(0, ge=0, description="Anos mínimos de experiência, se pedirem (\"técnico sênior, 5 anos\")")


class CriarTarefa(_Periodo):
    """Criar um serviço/tarefa novo e montar a equipe. Use quando pedirem para alocar gente a algo que ainda não está na lista de tarefas."""

    titulo: str = Field(description="Título curto do serviço")
    tipo: Literal["interna", "campo"] = Field(
        "interna", description="campo = em cliente, obra, viagem ou fora da empresa"
    )
    local: str | None = None
    contrato: str | None = Field(None, description="Código do contrato, se mencionado")
    descricao: str | None = None
    horas_por_pessoa: float | None = Field(None, gt=0, description="Total de horas de cada pessoa, só se disserem")
    prioridade: int = Field(3, ge=1, le=5, description="1 (baixa) a 5 (urgente). \"Urgente\" = 5. Não dito = 3")
    equipe: list[Vaga] = Field(description="Funções necessárias. Se não disserem quantas, use 1")


class AlocarTarefa(BaseModel):
    """Montar ou completar a equipe de uma tarefa que JÁ existe na lista."""

    tarefa_id: int


class RegistrarAusencia(_Periodo):
    """Registrar que um funcionário não estará disponível (férias, atestado, folga, treinamento, etc.)."""

    funcionario_id: int
    motivo: str = Field("outro", description="férias, atestado, folga, treinamento, outro...")


class TirarDaTarefa(BaseModel):
    """Retirar um funcionário de uma tarefa em que ele já está alocado (o sistema sugere substituto)."""

    tarefa_id: int
    funcionario_id: int


class SubstituirNaTarefa(BaseModel):
    """Trocar uma pessoa por outra ESPECÍFICA numa tarefa ("coloca o Beto no lugar da Ana na tarefa 1")."""

    tarefa_id: int
    sai_id: int = Field(description="id de quem sai")
    entra_id: int = Field(description="id de quem entra")


class IncluirNaTarefa(BaseModel):
    """Colocar uma pessoa ESPECÍFICA numa vaga aberta de uma tarefa que já existe ("coloca o Beto na tarefa 2").
    Use mesmo que ela não cumpra as regras: o sistema mostra o que falta e o gestor decide se força."""

    tarefa_id: int
    funcionario_id: int
    papel: str | None = Field(None, description="função da vaga, só se o gestor disser (\"como engenheiro\")")


class EncerrarTarefa(BaseModel):
    """Marcar uma tarefa como concluída ou cancelada."""

    tarefa_id: int
    status: Literal["concluida", "cancelada"]


class RemarcarTarefa(_Periodo):
    """Mudar a data ou o horário de uma tarefa que já existe. Horas não ditas mantêm as atuais."""

    tarefa_id: int


class ConsultarDisponibilidade(_Periodo):
    """Ver quem está livre num período."""

    papel: str | None = Field(None, description="Filtrar por função, se pedirem")


class ConsultarAgenda(BaseModel):
    """Ver a agenda de um funcionário (tarefas e ausências)."""

    funcionario_id: int
    dias: int = Field(14, ge=1, le=90, description="Quantos dias à frente")


class VerTarefa(BaseModel):
    """Ver detalhes e equipe de uma tarefa existente."""

    tarefa_id: int


class ListarTarefas(BaseModel):
    """Listar as tarefas em andamento."""

    incluir_encerradas: bool = Field(False, description="true para incluir concluídas e canceladas")


class ConsultarVencimentos(BaseModel):
    """Ver certificações (NR10, NR35, ASO...) vencidas ou que vencem em breve."""

    dias: int = Field(30, ge=1, le=365, description="Janela em dias à frente")


class ConsultarContrato(BaseModel):
    """Perguntas sobre o que um contrato diz (multas, EPI, exigências, prazos). Responde com o trecho do contrato."""

    codigo: str = Field(description="Código do contrato, como na lista de contratos")
    pergunta: str = Field(description="O que o usuário quer saber, em poucas palavras")


class Responder(BaseModel):
    """Responder em texto: para perguntar o que falta, cumprimentar, explicar o que você faz,
    ou responder perguntas sobre a equipe usando SOMENTE os dados fornecidos."""

    texto: str


FERRAMENTAS = [
    CriarTarefa, AlocarTarefa, IncluirNaTarefa, RegistrarAusencia, TirarDaTarefa, SubstituirNaTarefa, EncerrarTarefa,
    RemarcarTarefa, ConsultarDisponibilidade, ConsultarAgenda, VerTarefa, ListarTarefas,
    ConsultarVencimentos, ConsultarContrato, Responder,
]
POR_NOME = {f.__name__: f for f in FERRAMENTAS}
ESCRITA = {"CriarTarefa", "AlocarTarefa", "IncluirNaTarefa", "RegistrarAusencia", "TirarDaTarefa", "SubstituirNaTarefa",
           "EncerrarTarefa", "RemarcarTarefa"}
CONSULTA = {"ConsultarDisponibilidade", "ConsultarAgenda", "VerTarefa", "ListarTarefas",
            "ConsultarVencimentos", "ConsultarContrato"}


# ---------- Modo equipe: o próprio funcionário falando com o bot ----------


class MinhaAgenda(BaseModel):
    """Ver as próprias tarefas e ausências dos próximos dias."""

    dias: int = Field(14, ge=1, le=60)


class MinhaTarefa(BaseModel):
    """Detalhes de uma tarefa em que a pessoa está escalada (local, horário, equipe, contrato)."""

    tarefa_id: int | None = Field(None, description="id da tarefa, se a pessoa disse ou se dá para saber pela lista")
    data: str | None = Field(None, description="AAAA-MM-DD, se a pessoa falou de um dia (\"minha tarefa de amanhã\")")


class InformarAusencia(_Periodo):
    """Avisar que não vai poder trabalhar (atestado, imprevisto, consulta...). Vai para aprovação do gestor."""

    motivo: str = Field("outro", description="atestado, consulta, imprevisto, folga, outro...")


class ResponderEquipe(BaseModel):
    """Responder em texto: cumprimentar, explicar o que você faz ou perguntar o que falta."""

    texto: str


FERRAMENTAS_EQUIPE = [MinhaAgenda, MinhaTarefa, InformarAusencia, ResponderEquipe]
POR_NOME_EQUIPE = {f.__name__: f for f in FERRAMENTAS_EQUIPE}


def _hora(valor: str | None, padrao: time) -> time:
    """Aceita '08:00', '8:00', '8h', '14h30', '14:30:00'."""
    if not valor:
        return padrao
    m = re.fullmatch(r"(\d{1,2})(?:[:h](\d{2})?)?(?::\d{2})?", valor.strip().lower())
    if not m or int(m.group(1)) > 23 or int(m.group(2) or 0) > 59:
        raise ValueError(f"hora inválida '{valor}'")
    return time(int(m.group(1)), int(m.group(2) or 0))


def _data(valor: str) -> date:
    try:
        return date.fromisoformat(valor.strip())
    except ValueError as e:
        raise ValueError(f"data inválida '{valor}'") from e


def resolver_periodo(
    p: _Periodo, dia_inteiro: bool = False, atual: tuple[datetime, datetime] | None = None
) -> tuple[datetime, datetime]:
    """Converte o período do modelo em datas reais, com padrões previsíveis.

    dia_inteiro=True (ausências): sem horas informadas, vale do início do primeiro dia ao fim do último.
    atual=(inicio, fim) (remarcar): o que não foi dito mantém o horário e a duração em dias atuais.
    Senão: horário padrão 08:00-17:00.
    """
    d_ini = _data(p.data_inicio)
    if atual is not None:
        a_ini, a_fim = atual
        d_fim = _data(p.data_fim) if p.data_fim else d_ini + (a_fim.date() - a_ini.date())
        inicio = datetime.combine(d_ini, _hora(p.hora_inicio, a_ini.time()))
        fim = datetime.combine(d_fim, _hora(p.hora_fim, a_fim.time()))
    elif dia_inteiro and not p.hora_inicio and not p.hora_fim:
        d_fim = _data(p.data_fim) if p.data_fim else d_ini
        inicio, fim = datetime.combine(d_ini, time.min), datetime.combine(d_fim + timedelta(days=1), time.min)
    else:
        d_fim = _data(p.data_fim) if p.data_fim else d_ini
        inicio = datetime.combine(d_ini, _hora(p.hora_inicio, HORA_INICIO_PADRAO))
        fim = datetime.combine(d_fim, _hora(p.hora_fim, HORA_FIM_PADRAO))
    if fim <= inicio:
        raise ValueError("o fim ficou antes do início")
    return inicio, fim
