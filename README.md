# Alocador

Cadastro de funcionários, disponibilidade e alocação de pessoas em tarefas e idas a campo.

- **Fase 1:** cadastros, API, painel web e motor de alocação. Não usa LLM.
- **Fase 2:** agente LangGraph + modelo de IA (Gemini por padrão; Claude, OpenAI ou Ollama) e bot do Telegram. Usam os mesmos serviços do painel.
- **Fase 3 (parcial):** contratos com exigências que o motor aplica sozinho e consulta de cláusulas pelo bot.
- **Fase 4:** avisos para a equipe pelo Telegram, modo equipe no bot e resumo semanal de certificações.

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

Painel e bot podem rodar juntos no mesmo `alocacao.db`: o SQLite usa WAL e espera a escrita do outro
processo em vez de falhar com "database is locked".

## Rodar com Docker (Postgres + pgvector)

```bash
docker compose up -d --build
```

```bash
docker compose exec app python -m scripts.seed
```

Defina `POSTGRES_PASSWORD` e `SECRET_KEY` num arquivo `.env` (veja `.env.example`). Os containers rodam
com usuário sem privilégios, no fuso `America/Sao_Paulo`, e o `app` tem healthcheck em `/saude`
(que também testa o banco).

## Segurança do painel e da API

Sem configuração, o painel e a API ficam abertos (uso no próprio computador). Para expor na rede:

| Variável | Efeito |
|---|---|
| `PAINEL_SENHA` | O painel pede login (sessão de 7 dias em cookie assinado) |
| `API_TOKEN` | `/api/*` exige `Authorization: Bearer <token>` (botão **Authorize** em `/docs`) |
| `SECRET_KEY` | Assina sessões, avisos de tela e links de calendário. Sem ela, o app cria `.secret_key` |

Sempre ligado: todo formulário do painel leva um token CSRF, e POST vindo de outro site é bloqueado.
Os avisos de tela ("Proposta gerada") vão num cookie assinado de vida curta, não na URL.

## Agente e bot do Telegram

### Configurar (uma vez)

1. Copie `.env.example` para `.env`.
2. Crie uma chave do Gemini em https://aistudio.google.com/apikey num projeto **sem faturamento**.
   Projeto com faturamento sai do plano gratuito; no modo pré-pago sem saldo, toda chamada dá erro 402.
   Cole a chave em `GEMINI_API_KEY`. Prefere outro provedor? Veja [Trocar o modelo de IA](#trocar-o-modelo-de-ia).
3. No Telegram, fale com o **@BotFather** e mande `/newbot`. Cole o token em `TELEGRAM_BOT_TOKEN`.
4. Em `TELEGRAM_USUARIOS_PERMITIDOS`, coloque os **gestores**, separados por vírgula.
   O recomendado é o ID numérico: mande `/id` ao bot para descobrir o seu. `@usuario` também funciona,
   mas pode ser trocado e reaproveitado por outra pessoa, e avisos só chegam a IDs numéricos.
5. Para a **equipe** usar o bot, coloque o ID do Telegram de cada pessoa no cadastro dela (a pessoa manda
   `/id` ao bot). Ela precisa mandar `/start` uma vez para o bot poder escrever para ela.
6. Confira tudo de uma vez. O comando faz uma chamada de teste ao modelo de IA e não mostra segredos:

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

### Trocar o modelo de IA

O provedor é escolhido em `LLM_PROVIDER` no `.env`. Cada um tem a própria chave e o próprio modelo,
então dá para deixar várias chaves preenchidas e trocar só essa linha (depois, reinicie o bot e o painel).

| `LLM_PROVIDER` | Chave | Modelo (padrão) | Voz no Telegram |
|---|---|---|---|
| `gemini` (padrão) | `GEMINI_API_KEY` | `GEMINI_MODEL` (`gemini-3.5-flash-lite`) | sim |
| `anthropic` (ou `claude`) | `ANTHROPIC_API_KEY` | `ANTHROPIC_MODEL` (`claude-haiku-5-5`) | não |
| `openai` | `OPENAI_API_KEY` | `OPENAI_MODEL` (`gpt-5-mini`) | não |
| `ollama` (local, grátis) | — | `OLLAMA_MODEL` (`qwen3`) | não |

- **Voz:** só o Gemini ouve áudio na mesma chamada. Com os outros, o bot responde pedindo a mensagem em texto.
- **APIs compatíveis com a da OpenAI** (OpenRouter, Groq, DeepSeek, LM Studio...): use `LLM_PROVIDER=openai`,
  a URL em `OPENAI_BASE_URL`, a chave do serviço em `OPENAI_API_KEY` e o nome do modelo em `OPENAI_MODEL`.
- **Ollama:** instale em https://ollama.com e baixe o modelo antes (`ollama pull qwen3`). No Docker, o
  padrão já aponta para o Ollama do computador (`host.docker.internal:11434`).
- **Outro provedor do LangChain** (Mistral, Bedrock...): instale a integração e acrescente uma entrada em
  `PROVEDORES`, em `app/provedores.py`. O resto do app não muda: todos seguem a mesma interface de chat
  model do LangChain (`bind_tools`, `with_structured_output`).

Rode `python -m scripts.verificar` depois de trocar: ele mostra o provedor em uso e faz uma chamada de teste.

### O que o gestor pode pedir

| Mensagem | O que acontece |
|---|---|
| "preciso de 2 técnicos com NR35 pra quarta em Betim, SPDA, urgente" | Monta a prévia da equipe e pede confirmação |
| "o Diego está de atestado amanhã" | Mostra o que a ausência atinge, registra e sugere substitutos |
| "tira o Igor da tarefa 1" | Até 3 substitutos com botão, só retirar ou manter |
| "coloca o Beto no lugar da Ana na tarefa 1" | Confere as regras do Beto e pede confirmação; se ele viola alguma, oferece forçar |
| "coloca o Beto na tarefa 2", "põe a Helena mesmo sem NR10" | Coloca a pessoa numa vaga aberta; se ela viola alguma regra, oferece forçar |
| "completa a equipe da tarefa 2" | Propõe quem falta e pede confirmação |
| "passa a tarefa 2 pra quinta", "conclui a tarefa 3" | Mostra quem deixaria de poder e pede confirmação |
| "quem está livre sexta?", "agenda da Ana", "tarefas em andamento" | Responde direto |
| "quais NRs vencem este mês?", "o que o CT-2026-014 diz sobre multa?" | Responde direto, citando o trecho do contrato |

Também dá para mandar **áudio**: o Gemini ouve na mesma chamada, sem serviço de transcrição à parte
(com outros provedores, o bot pede a mensagem em texto).
Tudo que grava pede confirmação com botão. Também vale responder "sim", "não" ou o número da opção.
Se você mandar outro pedido enquanto um espera confirmação, o anterior é descartado.

**Forçar pelo bot.** Se a pessoa pedida não cumpre alguma regra (NR faltando ou vencida, jornada, descanso,
ausência, outra tarefa no horário...), o bot lista cada violação e oferece "Forçar mesmo assim". Ao tocar,
ele pergunta o motivo: a próxima mensagem é o motivo ("cancela" desiste). Só então grava, com o motivo na
alocação e na auditoria, como no painel. "Sim" digitado nunca força, e se aparecer uma violação que não
estava na prévia, nada é gravado.

**A equipe confirmada é a da prévia.** Se alguém da prévia ficou indisponível antes do clique, nada é
confirmado: o bot explica o que mudou e mostra uma prévia nova.

**Grupos.** Num grupo de gestores, cada pessoa tem a própria conversa com o bot: o "sim" de um não confirma
o pedido de outro, e só quem fez o pedido consegue apertar os botões dele.

### O que a equipe recebe e pode pedir

- **Avisos** quando é escalada, removida ou a tarefa muda de horário, com os botões **✔ Ciente** e
  **⚠ Tive um imprevisto**. O imprevisto avisa os gestores, com um botão que abre a escolha de substituto.
- **Modo equipe** (conversa privada com o bot): "minha agenda", "detalhes da minha tarefa de amanhã",
  "estou de atestado hoje". A ausência vira um pedido que um gestor aprova pelo bot ou pelo painel;
  ao aprovar, o bot já sugere substitutos para as tarefas atingidas. O modelo só vê os dados da própria pessoa.
- Comandos: `/agenda` (equipe), `/vencimentos` e `/cancelar` (gestores), `/id` (todos).

Toda segunda, a partir das 08h, os gestores recebem o resumo de certificações vencidas ou vencendo em
30 dias e dos pedidos de ausência parados. Desligue com `RESUMO_SEMANAL=0` ou `AVISOS_EQUIPE=0`.

### Como o agente funciona

```
início ─┬─> interpretar ─┬─> consultar ─────────────────────────────> fim
        │                ├─> preparar ─┬─> aprovar (pausa) ─> executar ─┬─> fim
        │                │      ▲      │                                ├─> preparar (equipe mudou)
        │                │      └──────┼────────────────────────────────┘
        │                │             └───────────────────────────────>┴─> substituir (pausa) ─> fim
        │                └─> fim (resposta direta ou pergunta)
        └─> substituir (imprevisto avisado pela equipe)
```

- **Uma chamada ao modelo por mensagem.** Só o passo "interpretar" usa o modelo. Ele escolhe uma intenção
  (criar tarefa, registrar ausência, consultar...) e preenche os campos.
- **Decisões em código.** Prévias, horários padrão (08h–17h, ou dia inteiro para ausências), escolha das
  pessoas e textos de resposta não gastam cota e não inventam dados.
- **Aprovação.** As pausas usam `interrupt()` do LangGraph. O estado fica em `agente.db`, então uma
  aprovação pendente sobrevive a reinícios do bot. Mensagens e botões da mesma conversa são processados
  em fila: dois toques rápidos não disputam o mesmo estado.
- **Contexto do modelo.** Recebe a data de hoje, a lista de funcionários (nome, função, especialidades,
  certificações **com validade**, marcando as vencidas), os contratos e as tarefas em andamento, das mais
  próximas para as mais distantes. **Não** recebe e-mail, ID do Telegram nem o texto livre de experiências.
- **Privacidade.** Os nomes dos funcionários (e os áudios) vão para o provedor escolhido. No plano gratuito
  do Gemini, o Google pode usar o que é enviado para melhorar os modelos. Com o Ollama, nada sai do computador.

## Painel

| Página | O que tem |
|---|---|
| `/` | Resumo, certificações vencendo em 30 dias, pedidos de ausência para aprovar, livres hoje |
| `/tarefas` | Todas as tarefas, inclusive concluídas e canceladas, com filtros e exportação CSV |
| `/tarefas/{id}` | Proposta, ranking, rejeitados e por quê, confirmados que deixaram de cumprir as regras, histórico |
| `/tarefas/{id}/editar` | Dados, período e vagas (botão "+ Adicionar função"); revalida quem já está confirmado |
| `/tarefas/{id}/os` | Ordem de serviço para imprimir ou salvar em PDF |
| `/semana` | Funcionários × dias: tarefas, propostas, ausências e horas da carga semanal |
| `/funcionarios/{id}` | Agenda, ausências, cadastro e o link `.ics` para assinar a agenda no celular |
| `/contratos` | Exigências por contrato, texto das cláusulas e extração das exigências com IA |
| `/auditoria` | Quem fez o quê; alocações forçadas contra as regras, com o motivo |

## Testes

```bash
.venv\Scripts\python -m pytest -q
```

## Como a alocação decide

Para cada vaga da tarefa (ex.: "2× técnico com Elétrica ≥ 3 e NR10"):

1. **Regras rígidas.** Quem falhar em qualquer uma é rejeitado, com o motivo registrado:
   - mesmo papel/função;
   - especialidades no nível mínimo;
   - certificações válidas até o fim da tarefa (mais as do contrato da tarefa);
   - experiência mínima (a maior entre a da vaga e a do contrato);
   - disponível para viagem (se a tarefa é de campo);
   - sem ausência no período;
   - sem outra tarefa confirmada no mesmo horário;
   - dentro da jornada diária e da carga semanal;
   - pelo menos 11h de descanso entre jornadas (CLT art. 66, `DESCANSO_MIN_HORAS`);
   - entre dois serviços de campo em cidades diferentes, tempo de deslocamento (`DESLOCAMENTO_MIN_MINUTOS`, 60).
2. **Pontuação de 0 a 100** dos que passaram:
   - aderência às especialidades exigidas (40);
   - especialidades desejáveis (15);
   - experiência (15);
   - folga de carga na semana (25), que espalha o trabalho pela equipe;
   - proximidade (5): em serviço de campo, a cidade base da pessoa é a do local.
3. **Escolha.** Fluxo de custo mínimo: primeiro o maior número possível de vagas preenchidas; entre as
   equipes que fazem isso, a de maior pontuação somada. Assim alguém versátil não é gasto numa vaga fácil.
   A mesma pessoa não ocupa duas vagas; quem ficou com outra vaga aparece nos rejeitados com esse motivo.

Em tarefas de prioridade 4 ou 5, quem tem o perfil mas está preso numa tarefa de prioridade pelo menos
2 níveis menor aparece como **candidato a remanejamento**, com a tarefa que precisaria ser liberada.

A proposta fica gravada como `proposta` até alguém confirmar. Na confirmação as regras são checadas
de novo. É possível forçar uma escolha que viola regras, pelo painel ou pelo bot; os dois pedem o motivo,
e isso fica na alocação e na auditoria.

Ao registrar uma ausência, editar ou remarcar uma tarefa, o sistema devolve quem já estava confirmado e
deixou de cumprir as regras. Ele não cancela nada sozinho.

**Horas:** numa tarefa de um dia sem horas informadas, conta a duração limitada a 8h (08h–17h = 8h).
Em tarefas de vários dias, cada dia conta o trecho dentro da jornada (08h–17h), até 8h: sair segunda às
15h conta 2h na segunda. Um trecho todo fora da jornada (plantão de 22h a 02h) conta pela duração real.
Sábado e domingo só entram se a tarefa começa ou termina neles (parada de sexta a domingo).
Com "horas por pessoa" informadas, o total é repartido na proporção desses trechos.

**Disponibilidade** ("quem está livre?"): sem ausência, sem tarefa no mesmo horário e com horas sobrando
no dia e na semana. Quem faz 30h por semana ou 6h por dia aparece livre se a agenda está vazia.

**Fuso:** todas as datas são horário local de `APP_TIMEZONE` (padrão `America/Sao_Paulo`), guardadas sem
fuso. Datas com fuso na API (`...Z`, `-03:00`) são convertidas na entrada.

## Contratos

Uma tarefa com o campo "Contrato" igual ao código de um contrato cadastrado exige de **toda a equipe** as
certificações e a experiência mínima do contrato; o motivo da rejeição diz que veio do contrato. Se o
contrato exige técnico de segurança e a tarefa não tem essa vaga, o painel e o bot avisam.

O texto das cláusulas fica no cadastro. O bot responde perguntas citando os trechos mais ligados à
pergunta (busca por palavras, sem embeddings: funciona em SQLite e Postgres, sem modelo local). No painel,
"Extrair exigências do texto com IA" lê o contrato com o modelo de IA uma vez e soma o que encontrar; revise antes.

## Estrutura

| Arquivo | O que faz |
|---|---|
| `app/allocation.py` | Motor de regras, pontuação e escolha (puro, sem LLM) |
| `app/services.py` | Regras de negócio: criar, propor, confirmar, substituir, editar, auditoria, avisos |
| `app/contratos.py` | Busca de cláusulas e extração de exigências |
| `app/agente/grafo.py` | Grafo LangGraph, aprovações e a classe `Agente` |
| `app/agente/equipe.py` | Modo equipe do bot (o próprio funcionário) |
| `app/agente/intencoes.py` | Intenções que o modelo pode escolher |
| `app/agente/llm.py` | Prompt, contexto e chamada ao modelo |
| `app/provedores.py` | Escolha do modelo de IA (Gemini, Claude, OpenAI, Ollama) e mensagens de erro |
| `app/agente/formatar.py` | Textos das respostas |
| `app/bot.py` | Bot do Telegram, avisos e resumo semanal |
| `app/api.py` | API REST em `/api` |
| `app/web.py` + `app/templates/` | Painel web |
| `app/seguranca.py` | Cookies assinados, senha e tokens de calendário |
| `app/relogio.py` | "Agora" e "hoje" no fuso do negócio |
| `app/models.py` | Tabelas (SQLAlchemy) |
| `scripts/seed.py` | Dados de exemplo |

## Principais rotas da API

| Método | Rota | Uso |
|---|---|---|
| POST | `/api/funcionarios` | Cadastrar funcionário |
| PUT | `/api/funcionarios/{id}` | Editar (com `ativo: true/false` para reativar ou desativar) |
| DELETE | `/api/funcionarios/{id}?forcar=` | Desativar; 409 se ainda estiver confirmado em tarefas futuras |
| POST | `/api/funcionarios/{id}/reativar` | Reativar |
| GET | `/api/funcionarios/disponiveis?inicio=&fim=&papel=` | Quem está livre |
| POST | `/api/funcionarios/{id}/indisponibilidades` | Registrar ausência (devolve tarefas afetadas) |
| GET | `/api/funcionarios/{id}/agenda?inicio=&fim=` | Agenda |
| GET | `/api/certificacoes/vencimentos?dias=30` | Certificações vencidas ou vencendo |
| GET | `/api/tarefas?status=&de=&ate=&tipo=&contrato=` | Listar tarefas com filtros (`status=ativas` também vale) |
| POST | `/api/tarefas` | Criar tarefa com vagas |
| PUT | `/api/tarefas/{id}` | Editar dados e vagas (devolve quem passou a violar regras) |
| POST | `/api/tarefas/{id}/remarcar` | Mudar o período (idem) |
| POST | `/api/tarefas/{id}/propor` | Gerar proposta (escolhidos, alternativas, rejeitados e motivos) |
| POST | `/api/tarefas/{id}/confirmar` | Confirmar proposta ou escolhas manuais (`forcar` + `motivo`) |
| DELETE | `/api/alocacoes/{id}` | Cancelar uma alocação (409 em tarefa concluída ou cancelada) |
| GET/POST/PUT/DELETE | `/api/contratos[/{codigo}]` | Contratos e suas exigências |
| GET | `/api/auditoria?acao=forcar` | Registro de ações |

## Próximos passos

- **Busca semântica nos contratos.** Hoje a busca é por palavras. Com muitos contratos, vale gerar
  embeddings (por exemplo `gemini-embedding-001`) e guardar no pgvector, que já está no Docker.
- **Migrações com Alembic.** Hoje as tabelas novas são criadas no start e os ajustes de banco antigo ficam
  em `app/db.py`; mudanças em colunas existentes vão precisar de migração.
- **Usuários e permissões.** O login atual é uma senha única de gestor.
- Coluna `empresa_id`, se virar SaaS.
