"""Conversa com o agente no terminal, sem Telegram. Útil para testar o prompt e o modelo.

Uso:  python -m app.agente.cli
Responda às confirmações com o número da opção, "sim" ou "não".
"""

import logging

from ..db import init_db
from . import formatar as fmt
from .grafo import Resultado, criar_agente

CONVERSA = "cli"


def mostrar(r: Resultado) -> None:
    for m in r.mensagens:
        print(f"\nagente> {m}")
    if r.pendente:
        print(f"\nagente> {r.pendente['texto']}")
        for i, (_, rotulo) in enumerate(r.pendente["opcoes"], 1):
            print(f"   [{i}] {rotulo}")


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    init_db()
    agente = criar_agente()
    print(fmt.ajuda())
    print('\n(digite "sair" para encerrar)')
    while True:
        try:
            texto = input("\nvocê> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if texto.lower() in ("sair", "exit", "quit"):
            break
        if texto:
            mostrar(agente.enviar(CONVERSA, texto))


if __name__ == "__main__":
    main()
