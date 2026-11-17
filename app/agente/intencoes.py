"""Intenções que o modelo pode escolher. Cada classe vira uma "ferramenta" para o Gemini.

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


class CriarTarefa(_Periodo):
    """Criar um serviço/tarefa novo e montar a equipe. Use quando pedirem para alocar gente a algo que ainda não está na lista de tarefas."""

    titulo: str = Field(description="Título curto do serviço")
    tipo: Literal["interna", "campo"] = Field(
        "interna", description="campo = em cliente, obra, viagem ou fora da empresa"
    )
    local: str | None = None
    contrato: str | None = Field(None, description="Código do contrato, se mencionado")
    descricao: str | None = None
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


class Responder(BaseModel):
    """Responder em texto: para perguntar o que falta, cumprimentar, explicar o que você faz,
    ou responder perguntas sobre a equipe usando SOMENTE os dados fornecidos."""

    texto: str


FERRAMENTAS = [
    CriarTarefa, AlocarTarefa, RegistrarAusencia, TirarDaTarefa,
    ConsultarDisponibilidade, ConsultarAgenda, VerTarefa, ListarTarefas, Responder,
]
POR_NOME = {f.__name__: f for f in FERRAMENTAS}
ESCRITA = {"CriarTarefa", "AlocarTarefa", "RegistrarAusencia", "TirarDaTarefa"}
CONSULTA = {"ConsultarDisponibilidade", "ConsultarAgenda", "VerTarefa", "ListarTarefas"}


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


def resolver_periodo(p: _Periodo, dia_inteiro: bool = False) -> tuple[datetime, datetime]:
    """Converte o período do modelo em datas reais, com padrões previsíveis.

    dia_inteiro=True (ausências): sem horas informadas, vale do início do primeiro dia ao fim do último.
    Senão: horário padrão 08:00-17:00.
    """
    d_ini = _data(p.data_inicio)
    d_fim = _data(p.data_fim) if p.data_fim else d_ini
    if dia_inteiro and not p.hora_inicio and not p.hora_fim:
        inicio, fim = datetime.combine(d_ini, time.min), datetime.combine(d_fim + timedelta(days=1), time.min)
    else:
        inicio = datetime.combine(d_ini, _hora(p.hora_inicio, HORA_INICIO_PADRAO))
        fim = datetime.combine(d_fim, _hora(p.hora_fim, HORA_FIM_PADRAO))
    if fim <= inicio:
        raise ValueError("o fim ficou antes do início")
    return inicio, fim
