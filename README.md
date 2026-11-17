# Alocador

Cadastro de funcionários, disponibilidade e alocação de pessoas em tarefas e idas a campo.

- **Fase 1:** cadastros, API, painel web e motor de alocação. Não usa LLM.
- **Fase 2:** agente LangGraph + Gemini e bot do Telegram. Usam os mesmos serviços do painel.

## Rodar sem Docker (SQLite)

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python -m pip install -r requirements.txt
```

```bash
.venv\Scripts\python -m scripts.seed
```

```bash
.venv\Scripts\python -m uvicorn app.main:app --reload
```

- Painel: http://localhost:8000
- Documentação da API: http://localhost:8000/docs
- `python -m scripts.seed --reset` apaga o banco e recria os dados de exemplo.

## Rodar com Docker (Postgres + pgvector)

```bash
docker compose up -d --build
```

```bash
docker compose exec app python -m scripts.seed
```

Defina `POSTGRES_PASSWORD` num arquivo `.env` (veja `.env.example`).

## Agente e bot do Telegram

### Configurar (uma vez)

1. Copie `.env.example` para `.env`.
2. Crie uma chave do Gemini em https://aistudio.google.com/apikey num projeto **sem faturamento**.
   Projeto com faturamento sai do plano gratuito; no modo pré-pago sem saldo, toda chamada dá erro 402.
   Cole a chave em `GEMINI_API_KEY`.
3. No Telegram, fale com o **@BotFather** e mande `/newbot`. Cole o token em `TELEGRAM_BOT_TOKEN`.
4. Em `TELEGRAM_USUARIOS_PERMITIDOS`, coloque quem pode usar o bot, separado por vírgula.
   O recomendado é o ID numérico: mande `/id` ao bot para descobrir o seu. `@usuario` também funciona,
   mas pode ser trocado e reaproveitado por outra pessoa.
5. Confira tudo de uma vez. O comando faz uma chamada de teste ao Gemini e não mostra segredos:

```bash
.venv\Scripts\python -m scripts.verificar
```

### Rodar

Testar o agente no terminal, sem Telegram:

```bash
.venv\Scripts\python -m app.agente.cli
```

Bot do Telegram:

```bash
.venv\Scripts\python -m app.bot
```

Com Docker, o `docker compose up -d` já sobe o bot junto, lendo o `.env`.

### O que dá para pedir

| Mensagem | O que acontece |
|---|---|
| "preciso de 2 técnicos com NR35 pra quarta em Betim, SPDA" | Monta a prévia da equipe e pede confirmação |
| "o Diego está de atestado amanhã" | Mostra o que a ausência atinge, registra e sugere substitutos |
| "tira o Igor da tarefa 1" | Sugere substituto: trocar, só retirar ou manter |
| "completa a equipe da tarefa 2" | Propõe quem falta e pede confirmação |
| "quem está livre sexta?", "agenda da Ana", "tarefas em andamento" | Responde direto |

Tudo que grava pede confirmação com botão. Também vale responder "sim", "não" ou o número da opção.
Se você mandar outro pedido enquanto um espera confirmação, o anterior é descartado.

Se o vínculo com o Telegram estiver preenchido no cadastro do funcionário, frases como "não vou amanhã" já
entendem quem é "eu".

### Como o agente funciona

```
interpretar ─┬─> consultar ─────────────────────────────> fim
             ├─> preparar ─┬─> aprovar (pausa) ─> executar ─┬─> fim
             │             └──────────────────────────────>┴─> substituir (pausa, 1 por vez) ─> fim
             └─> fim (resposta direta ou pergunta)
```

- **Uma chamada ao Gemini por mensagem.** Só o passo "interpretar" usa o modelo. Ele escolhe uma intenção
  (criar tarefa, registrar ausência, consultar...) e preenche os campos.
- **Decisões em código.** Prévias, horários padrão (08h–17h, ou dia inteiro para ausências), escolha das
  pessoas e textos de resposta não gastam cota e não inventam dados.
- **Aprovação.** As pausas usam `interrupt()` do LangGraph. O estado fica em `agente.db`, então uma
  aprovação pendente sobrevive a reinícios do bot.
- **Contexto do modelo.** Recebe a data de hoje, a lista de funcionários (nome, função, especialidades,
  certificações) e as tarefas em andamento. **Não** recebe e-mail, ID do Telegram nem o texto livre de
  experiências.
- **Privacidade.** Os nomes dos funcionários vão para o Google. No plano gratuito do Gemini, o Google pode
  usar o que é enviado para melhorar os modelos.

## Testes

```bash
.venv\Scripts\python -m pytest -q
```

## Como a alocação decide

Para cada vaga da tarefa (ex.: "2× técnico com Elétrica ≥ 3 e NR10"):

1. **Regras rígidas.** Quem falhar em qualquer uma é rejeitado, com o motivo registrado:
   - mesmo papel/função;
   - especialidades no nível mínimo;
   - certificações válidas até o fim da tarefa;
   - experiência mínima;
   - disponível para viagem (se a tarefa é de campo);
   - sem ausência no período;
   - sem outra tarefa confirmada no mesmo horário;
   - dentro da jornada diária e da carga semanal.
2. **Pontuação de 0 a 100** dos que passaram:
   - aderência às especialidades exigidas (40);
   - especialidades desejáveis (15);
   - experiência (20);
   - folga de carga na semana (25), que espalha o trabalho pela equipe.
3. **Escolha.** As vagas com menos candidatos viáveis são preenchidas primeiro, para que alguém versátil não
   seja gasto numa vaga fácil. A mesma pessoa não ocupa duas vagas.

A proposta fica gravada como `proposta` até alguém confirmar. Na confirmação as regras são checadas
de novo. É possível forçar uma escolha que viola regras; isso fica registrado na alocação.

Ao registrar uma ausência, o sistema devolve as tarefas confirmadas que ela atinge. Ele não cancela
nada sozinho.

**Horas:** numa tarefa de um dia sem horas informadas, conta a duração limitada a 8h (08h–17h = 8h).
Em tarefas de vários dias contam 8h por dia útil. Informe "horas por pessoa" para outro valor.

## Estrutura

| Arquivo | O que faz |
|---|---|
| `app/allocation.py` | Motor de regras e pontuação (puro, sem LLM) |
| `app/services.py` | Regras de negócio: criar, propor, confirmar, substituir. Usadas pelo painel e pelo agente |
| `app/agente/grafo.py` | Grafo LangGraph, aprovações e a classe `Agente` |
| `app/agente/intencoes.py` | Intenções que o modelo pode escolher |
| `app/agente/llm.py` | Prompt, contexto e chamada ao Gemini |
| `app/agente/formatar.py` | Textos das respostas |
| `app/bot.py` | Bot do Telegram |
| `app/api.py` | API REST em `/api` |
| `app/web.py` + `app/templates/` | Painel web |
| `app/models.py` | Tabelas (SQLAlchemy) |
| `scripts/seed.py` | Dados de exemplo |

## Principais rotas da API

| Método | Rota | Uso |
|---|---|---|
| POST | `/api/funcionarios` | Cadastrar funcionário |
| GET | `/api/funcionarios/disponiveis?inicio=&fim=&papel=` | Quem está livre |
| POST | `/api/funcionarios/{id}/indisponibilidades` | Registrar ausência (devolve tarefas afetadas) |
| GET | `/api/funcionarios/{id}/agenda?inicio=&fim=` | Agenda |
| POST | `/api/tarefas` | Criar tarefa com vagas |
| POST | `/api/tarefas/{id}/propor` | Gerar proposta (escolhidos, alternativas, rejeitados e motivos) |
| POST | `/api/tarefas/{id}/confirmar` | Confirmar proposta ou escolhas manuais |
| DELETE | `/api/alocacoes/{id}` | Cancelar uma alocação |

## Próximas fases

3. **RAG.** Contratos e políticas em pgvector, com embeddings locais. Exigências críticas viram regras estruturadas.
4. **Avisos.** Notificar os funcionários pelo Telegram quando forem alocados ou trocados.
   A sugestão de substitutos já entrou na fase 2.

Pendências conhecidas:
- Login e permissões no painel.
- Migrações com Alembic (hoje as tabelas são criadas no start).
- Coluna `empresa_id`, se virar SaaS.
