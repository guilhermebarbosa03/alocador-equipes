import hmac
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from . import config, schemas
from . import services as sv
from .allocation import contrato_por_codigo
from .allocation import disponiveis as calc_disponiveis
from .db import get_db
from .relogio import local

_bearer = HTTPBearer(auto_error=False, description="Valor de API_TOKEN (só exigido se API_TOKEN estiver definido)")
AUTOR = "api"


def exigir_token(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    if not config.API_TOKEN:
        return
    if cred is None or not hmac.compare_digest(cred.credentials.encode(), config.API_TOKEN.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token da API inválido ou ausente",
                            headers={"WWW-Authenticate": "Bearer"})


router = APIRouter(dependencies=[Depends(exigir_token)])


def _intervalo(inicio: datetime, fim: datetime) -> tuple[datetime, datetime]:
    inicio, fim = local(inicio), local(fim)
    if fim <= inicio:
        raise sv.ErroNegocio("fim deve ser depois do início")
    return inicio, fim


# ---------- Funcionários ----------


@router.get("/funcionarios", response_model=list[schemas.FuncionarioOut], tags=["funcionários"])
def listar_funcionarios(incluir_inativos: bool = False, papel: str | None = None, db: Session = Depends(get_db)):
    return sv.listar_funcionarios(db, incluir_inativos, papel)


@router.post(
    "/funcionarios", response_model=schemas.FuncionarioOut, status_code=status.HTTP_201_CREATED, tags=["funcionários"]
)
def criar_funcionario(dados: schemas.FuncionarioIn, db: Session = Depends(get_db)):
    return sv.criar_funcionario(db, dados)


@router.get("/funcionarios/disponiveis", response_model=list[schemas.CandidatoOut], tags=["funcionários"])
def funcionarios_disponiveis(
    inicio: datetime, fim: datetime, papel: str | None = None, db: Session = Depends(get_db)
):
    """Quem está livre no intervalo (sem ausência, sem conflito e com horas sobrando no dia e na semana)."""
    return [a.to_dict() for a in calc_disponiveis(db, *_intervalo(inicio, fim), papel)]


@router.get("/funcionarios/{funcionario_id}", response_model=schemas.FuncionarioOut, tags=["funcionários"])
def obter_funcionario(funcionario_id: int, db: Session = Depends(get_db)):
    return sv.obter_funcionario(db, funcionario_id)


@router.put("/funcionarios/{funcionario_id}", response_model=schemas.FuncionarioOut, tags=["funcionários"])
def atualizar_funcionario(funcionario_id: int, dados: schemas.FuncionarioIn, db: Session = Depends(get_db)):
    """`ativo: true` reativa e `ativo: false` desativa (mesmo com alocações futuras). Sem `ativo`, mantém."""
    return sv.atualizar_funcionario(db, sv.obter_funcionario(db, funcionario_id), dados, AUTOR)


@router.delete("/funcionarios/{funcionario_id}", response_model=schemas.FuncionarioDesativado, tags=["funcionários"])
def desativar_funcionario(funcionario_id: int, forcar: bool = False, db: Session = Depends(get_db)):
    """Desativa (não apaga) para manter o histórico. Se a pessoa ainda está confirmada em tarefas futuras,
    responde 409 com a lista; `forcar=true` desativa mesmo assim (as alocações continuam até alguém substituir)."""
    f = sv.obter_funcionario(db, funcionario_id)
    futuras = sv.desativar_funcionario(db, f, forcar, AUTOR)
    return {"funcionario": f, "alocacoes_futuras": futuras}


@router.post("/funcionarios/{funcionario_id}/reativar", response_model=schemas.FuncionarioOut, tags=["funcionários"])
def reativar_funcionario(funcionario_id: int, db: Session = Depends(get_db)):
    return sv.reativar_funcionario(db, sv.obter_funcionario(db, funcionario_id), AUTOR)


@router.get(
    "/funcionarios/{funcionario_id}/indisponibilidades",
    response_model=list[schemas.IndisponibilidadeOut],
    tags=["disponibilidade"],
)
def listar_indisponibilidades(funcionario_id: int, db: Session = Depends(get_db)):
    f = sv.obter_funcionario(db, funcionario_id)
    return sorted(f.indisponibilidades, key=lambda i: i.inicio)


@router.post(
    "/funcionarios/{funcionario_id}/indisponibilidades",
    response_model=schemas.IndisponibilidadeCriada,
    status_code=status.HTTP_201_CREATED,
    tags=["disponibilidade"],
)
def criar_indisponibilidade(
    funcionario_id: int, dados: schemas.IndisponibilidadeIn, db: Session = Depends(get_db)
):
    """Registra ausência e devolve as alocações confirmadas que ela atinge."""
    ind, afetadas = sv.criar_indisponibilidade(db, sv.obter_funcionario(db, funcionario_id), dados, AUTOR)
    return {"indisponibilidade": ind, "alocacoes_afetadas": afetadas}


@router.delete(
    "/indisponibilidades/{indisponibilidade_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["disponibilidade"]
)
def remover_indisponibilidade(indisponibilidade_id: int, db: Session = Depends(get_db)):
    sv.remover_indisponibilidade(db, indisponibilidade_id)


@router.get(
    "/funcionarios/{funcionario_id}/agenda", response_model=list[schemas.EventoAgenda], tags=["disponibilidade"]
)
def agenda(
    funcionario_id: int,
    inicio: datetime = Query(...),
    fim: datetime = Query(...),
    db: Session = Depends(get_db),
):
    return sv.agenda(db, sv.obter_funcionario(db, funcionario_id), *_intervalo(inicio, fim))


@router.get("/certificacoes/vencimentos", response_model=list[schemas.Vencimento], tags=["funcionários"])
def vencimentos(dias: int = Query(30, ge=1, le=365), db: Session = Depends(get_db)):
    """Certificações vencidas ou que vencem nos próximos `dias` (funcionários ativos)."""
    return sv.vencimentos(db, dias)


# ---------- Tarefas ----------


@router.get("/tarefas", response_model=list[schemas.TarefaOut], tags=["tarefas"])
def listar_tarefas(
    status: str | None = Query(None, description="aberta, proposta, parcial, alocada, concluida, cancelada ou ativas"),
    de: date | None = None, ate: date | None = None, tipo: str | None = None, contrato: str | None = None,
    db: Session = Depends(get_db),
):
    return sv.listar_tarefas(db, status, de, ate, tipo, contrato)


@router.post("/tarefas", response_model=schemas.TarefaOut, status_code=status.HTTP_201_CREATED, tags=["tarefas"])
def criar_tarefa(dados: schemas.TarefaIn, db: Session = Depends(get_db)):
    return sv.criar_tarefa(db, dados)


@router.get("/tarefas/{tarefa_id}", response_model=schemas.TarefaOut, tags=["tarefas"])
def obter_tarefa(tarefa_id: int, db: Session = Depends(get_db)):
    return sv.obter_tarefa(db, tarefa_id)


def _alterada(t, problemas) -> dict:
    return {"tarefa": t, "problemas": {a.funcionario_nome: m for a, m in problemas}}


@router.put("/tarefas/{tarefa_id}", response_model=schemas.TarefaAlterada, tags=["tarefas"])
def editar_tarefa(tarefa_id: int, dados: schemas.TarefaIn, db: Session = Depends(get_db)):
    """Vagas existentes vêm com `id`; sem `id` cria vaga nova; vaga que não vier é removida.
    Devolve, em `problemas`, quem já estava confirmado e passou a violar regras (ninguém é tirado sozinho)."""
    t = sv.obter_tarefa(db, tarefa_id)
    return _alterada(t, sv.editar_tarefa(db, t, dados, AUTOR))


@router.post("/tarefas/{tarefa_id}/remarcar", response_model=schemas.TarefaAlterada, tags=["tarefas"])
def remarcar_tarefa(tarefa_id: int, dados: schemas.RemarcarIn, db: Session = Depends(get_db)):
    t = sv.obter_tarefa(db, tarefa_id)
    return _alterada(t, sv.remarcar_tarefa(db, t, dados.inicio, dados.fim, AUTOR))


@router.get("/tarefas/{tarefa_id}/alocacoes", response_model=list[schemas.AlocacaoOut], tags=["alocação"])
def listar_alocacoes(tarefa_id: int, incluir_canceladas: bool = False, db: Session = Depends(get_db)):
    t = sv.obter_tarefa(db, tarefa_id)
    return [a for a in t.alocacoes if incluir_canceladas or a.status != "cancelada"]


@router.post("/tarefas/{tarefa_id}/propor", response_model=schemas.PropostaOut, tags=["alocação"])
def propor(tarefa_id: int, db: Session = Depends(get_db)):
    """Roda o motor: escolhe os melhores por vaga, lista alternativas e explica cada rejeição."""
    return sv.gerar_proposta(db, sv.obter_tarefa(db, tarefa_id)).to_dict()


@router.post("/tarefas/{tarefa_id}/confirmar", response_model=list[schemas.AlocacaoOut], tags=["alocação"])
def confirmar(tarefa_id: int, dados: schemas.ConfirmarIn | None = None, db: Session = Depends(get_db)):
    dados = dados or schemas.ConfirmarIn()
    return sv.confirmar(db, sv.obter_tarefa(db, tarefa_id), dados.escolhas, dados.forcar, dados.motivo, AUTOR)


@router.post("/tarefas/{tarefa_id}/concluir", response_model=schemas.TarefaOut, tags=["tarefas"])
def concluir(tarefa_id: int, db: Session = Depends(get_db)):
    return sv.encerrar_tarefa(db, sv.obter_tarefa(db, tarefa_id), "concluida", AUTOR)


@router.post("/tarefas/{tarefa_id}/cancelar", response_model=schemas.TarefaOut, tags=["tarefas"])
def cancelar(tarefa_id: int, db: Session = Depends(get_db)):
    return sv.encerrar_tarefa(db, sv.obter_tarefa(db, tarefa_id), "cancelada", AUTOR)


@router.delete("/alocacoes/{alocacao_id}", response_model=schemas.AlocacaoOut, tags=["alocação"])
def cancelar_alocacao(alocacao_id: int, db: Session = Depends(get_db)):
    """Recusa (409) se a tarefa já foi concluída ou cancelada: o histórico não muda."""
    return sv.cancelar_alocacao(db, sv.obter_alocacao(db, alocacao_id), AUTOR)


# ---------- Contratos e auditoria ----------


@router.get("/contratos", response_model=list[schemas.ContratoOut], tags=["contratos"])
def listar_contratos(db: Session = Depends(get_db)):
    return sv.listar_contratos(db)


@router.post("/contratos", response_model=schemas.ContratoOut, status_code=status.HTTP_201_CREATED, tags=["contratos"])
def criar_contrato(dados: schemas.ContratoIn, db: Session = Depends(get_db)):
    """Tarefas com `contrato` igual ao código passam a exigir estas certificações e experiência de toda a equipe."""
    return sv.salvar_contrato(db, dados, autor=AUTOR)


def _contrato(db: Session, codigo: str):
    c = contrato_por_codigo(db, codigo)
    if c is None:
        raise sv.ErroNegocio(f"Contrato {codigo} não encontrado", 404)
    return c


@router.get("/contratos/{codigo}", response_model=schemas.ContratoOut, tags=["contratos"])
def obter_contrato(codigo: str, db: Session = Depends(get_db)):
    return _contrato(db, codigo)


@router.put("/contratos/{codigo}", response_model=schemas.ContratoOut, tags=["contratos"])
def atualizar_contrato(codigo: str, dados: schemas.ContratoIn, db: Session = Depends(get_db)):
    return sv.salvar_contrato(db, dados, _contrato(db, codigo), AUTOR)


@router.delete("/contratos/{codigo}", status_code=status.HTTP_204_NO_CONTENT, tags=["contratos"])
def remover_contrato(codigo: str, db: Session = Depends(get_db)):
    sv.remover_contrato(db, _contrato(db, codigo), AUTOR)


@router.get("/auditoria", response_model=list[schemas.AuditoriaOut], tags=["auditoria"])
def auditoria(acao: str | None = None, tarefa_id: int | None = None, limite: int = Query(200, ge=1, le=1000),
              db: Session = Depends(get_db)):
    """Registro de ações. `acao=forcar` lista as alocações confirmadas contra as regras e o motivo."""
    return sv.listar_auditoria(db, acao, tarefa_id, limite)
