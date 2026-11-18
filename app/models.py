from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .relogio import agora


class Funcionario(Base):
    __tablename__ = "funcionarios"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(120))
    papel: Mapped[str] = mapped_column(String(60), index=True)  # tecnico, engenheiro, eletricista...
    email: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # ID numérico do Telegram: o bot reconhece a pessoa e manda avisos. Consultado a cada mensagem.
    telegram_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    carga_horaria_semanal: Mapped[float] = mapped_column(Float, default=44)
    horas_max_dia: Mapped[float] = mapped_column(Float, default=8)
    anos_experiencia: Mapped[float] = mapped_column(Float, default=0)
    disponivel_viagem: Mapped[bool] = mapped_column(Boolean, default=True)
    base: Mapped[str | None] = mapped_column(String(120), nullable=True)
    resumo: Mapped[str | None] = mapped_column(Text, nullable=True)  # experiências em texto livre
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)

    especialidades: Mapped[list["Especialidade"]] = relationship(
        back_populates="funcionario", cascade="all, delete-orphan", lazy="selectin"
    )
    certificacoes: Mapped[list["Certificacao"]] = relationship(
        back_populates="funcionario", cascade="all, delete-orphan", lazy="selectin"
    )
    indisponibilidades: Mapped[list["Indisponibilidade"]] = relationship(
        back_populates="funcionario", cascade="all, delete-orphan"
    )


class Especialidade(Base):
    __tablename__ = "especialidades"

    id: Mapped[int] = mapped_column(primary_key=True)
    funcionario_id: Mapped[int] = mapped_column(ForeignKey("funcionarios.id", ondelete="CASCADE"))
    nome: Mapped[str] = mapped_column(String(80))
    nivel: Mapped[int] = mapped_column(Integer, default=3)  # 1 (básico) a 5 (especialista)

    funcionario: Mapped[Funcionario] = relationship(back_populates="especialidades")


class Certificacao(Base):
    __tablename__ = "certificacoes"

    id: Mapped[int] = mapped_column(primary_key=True)
    funcionario_id: Mapped[int] = mapped_column(ForeignKey("funcionarios.id", ondelete="CASCADE"))
    nome: Mapped[str] = mapped_column(String(80))
    validade: Mapped[date | None] = mapped_column(Date, nullable=True)  # None = não expira

    funcionario: Mapped[Funcionario] = relationship(back_populates="certificacoes")


class Indisponibilidade(Base):
    __tablename__ = "indisponibilidades"

    id: Mapped[int] = mapped_column(primary_key=True)
    funcionario_id: Mapped[int] = mapped_column(ForeignKey("funcionarios.id", ondelete="CASCADE"), index=True)
    inicio: Mapped[datetime] = mapped_column(DateTime)
    fim: Mapped[datetime] = mapped_column(DateTime)
    motivo: Mapped[str] = mapped_column(String(120), default="outro")

    funcionario: Mapped[Funcionario] = relationship(back_populates="indisponibilidades")


class Tarefa(Base):
    __tablename__ = "tarefas"

    id: Mapped[int] = mapped_column(primary_key=True)
    titulo: Mapped[str] = mapped_column(String(160))
    descricao: Mapped[str | None] = mapped_column(Text, nullable=True)
    tipo: Mapped[str] = mapped_column(String(20), default="interna")  # interna | campo
    local: Mapped[str | None] = mapped_column(String(160), nullable=True)
    inicio: Mapped[datetime] = mapped_column(DateTime, index=True)
    fim: Mapped[datetime] = mapped_column(DateTime, index=True)
    horas_por_pessoa: Mapped[float | None] = mapped_column(Float, nullable=True)
    prioridade: Mapped[int] = mapped_column(Integer, default=3)
    contrato: Mapped[str | None] = mapped_column(String(120), nullable=True)  # código em Contrato.codigo
    # aberta | proposta | parcial | alocada | concluida | cancelada
    status: Mapped[str] = mapped_column(String(20), default="aberta")
    criada_em: Mapped[datetime] = mapped_column(DateTime, default=agora)

    requisitos: Mapped[list["Requisito"]] = relationship(
        back_populates="tarefa", cascade="all, delete-orphan", lazy="selectin", order_by="Requisito.id"
    )
    alocacoes: Mapped[list["Alocacao"]] = relationship(
        back_populates="tarefa", cascade="all, delete-orphan", lazy="selectin", order_by="Alocacao.id"
    )


class Requisito(Base):
    """Uma "vaga" da tarefa: quantas pessoas de um papel, com quais exigências."""

    __tablename__ = "requisitos"

    id: Mapped[int] = mapped_column(primary_key=True)
    tarefa_id: Mapped[int] = mapped_column(ForeignKey("tarefas.id", ondelete="CASCADE"))
    papel: Mapped[str] = mapped_column(String(60))
    quantidade: Mapped[int] = mapped_column(Integer, default=1)
    especialidades: Mapped[list] = mapped_column(JSON, default=list)  # [{"nome": ..., "nivel_min": ...}]
    desejaveis: Mapped[list] = mapped_column(JSON, default=list)  # ["automacao", ...]
    certificacoes: Mapped[list] = mapped_column(JSON, default=list)  # ["NR10", ...]
    anos_experiencia_min: Mapped[float] = mapped_column(Float, default=0)

    tarefa: Mapped[Tarefa] = relationship(back_populates="requisitos")


class Alocacao(Base):
    __tablename__ = "alocacoes"

    id: Mapped[int] = mapped_column(primary_key=True)
    tarefa_id: Mapped[int] = mapped_column(ForeignKey("tarefas.id", ondelete="CASCADE"), index=True)
    requisito_id: Mapped[int | None] = mapped_column(
        ForeignKey("requisitos.id", ondelete="SET NULL"), nullable=True
    )
    funcionario_id: Mapped[int] = mapped_column(ForeignKey("funcionarios.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="proposta")  # proposta | confirmada | cancelada
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    justificativa: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    criada_em: Mapped[datetime] = mapped_column(DateTime, default=agora)

    tarefa: Mapped[Tarefa] = relationship(back_populates="alocacoes")
    funcionario: Mapped[Funcionario] = relationship(lazy="joined")

    @property
    def funcionario_nome(self) -> str:
        return self.funcionario.nome if self.funcionario else f"#{self.funcionario_id}"


class Contrato(Base):
    """Exigências de um contrato. Toda tarefa com `contrato` igual ao código segue estas regras."""

    __tablename__ = "contratos"

    id: Mapped[int] = mapped_column(primary_key=True)
    codigo: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    cliente: Mapped[str | None] = mapped_column(String(160), nullable=True)
    certificacoes: Mapped[list] = mapped_column(JSON, default=list)  # obrigatórias para toda a equipe
    anos_experiencia_min: Mapped[float] = mapped_column(Float, default=0)
    exige_tecnico_seguranca: Mapped[bool] = mapped_column(Boolean, default=False)
    observacoes: Mapped[str | None] = mapped_column(Text, nullable=True)
    texto: Mapped[str | None] = mapped_column(Text, nullable=True)  # cláusulas, para consulta pelo agente
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, default=agora, onupdate=agora)


class Auditoria(Base):
    """Quem fez o quê. Guarda principalmente alocações forçadas contra as regras e o motivo."""

    __tablename__ = "auditoria"

    id: Mapped[int] = mapped_column(primary_key=True)
    quando: Mapped[datetime] = mapped_column(DateTime, default=agora, index=True)
    acao: Mapped[str] = mapped_column(String(40), index=True)
    autor: Mapped[str] = mapped_column(String(120), default="sistema")
    tarefa_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    funcionario_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    resumo: Mapped[str] = mapped_column(String(400))
    detalhes: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Notificacao(Base):
    """Caixa de saída de avisos para a equipe. O bot lê, envia pelo Telegram e grava a resposta."""

    __tablename__ = "notificacoes"

    id: Mapped[int] = mapped_column(primary_key=True)
    funcionario_id: Mapped[int] = mapped_column(ForeignKey("funcionarios.id", ondelete="CASCADE"), index=True)
    alocacao_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tarefa_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tipo: Mapped[str] = mapped_column(String(30))  # alocado | removido | remarcado
    texto: Mapped[str] = mapped_column(Text)
    criada_em: Mapped[datetime] = mapped_column(DateTime, default=agora, index=True)
    enviada_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    situacao: Mapped[str] = mapped_column(String(20), default="pendente")  # pendente | enviada | falhou | expirada
    resposta: Mapped[str | None] = mapped_column(String(20), nullable=True)  # ciente | imprevisto
    respondida_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    funcionario: Mapped[Funcionario] = relationship(lazy="joined")


class SolicitacaoAusencia(Base):
    """Ausência informada pelo próprio funcionário no Telegram, esperando aprovação de um gestor."""

    __tablename__ = "solicitacoes_ausencia"

    id: Mapped[int] = mapped_column(primary_key=True)
    funcionario_id: Mapped[int] = mapped_column(ForeignKey("funcionarios.id", ondelete="CASCADE"), index=True)
    inicio: Mapped[datetime] = mapped_column(DateTime)
    fim: Mapped[datetime] = mapped_column(DateTime)
    motivo: Mapped[str] = mapped_column(String(120), default="outro")
    status: Mapped[str] = mapped_column(String(20), default="pendente", index=True)  # pendente|aprovada|recusada
    criada_em: Mapped[datetime] = mapped_column(DateTime, default=agora)
    decidida_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decidida_por: Mapped[str | None] = mapped_column(String(120), nullable=True)
    avisada_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # gestores avisados pelo bot
    retorno_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # funcionário avisado da decisão

    funcionario: Mapped[Funcionario] = relationship(lazy="joined")
