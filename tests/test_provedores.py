"""Escolha do modelo de IA: cada provedor monta o chat model certo, sem chamar a API."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app import config, provedores
from app.agente.intencoes import FERRAMENTAS, FERRAMENTAS_EQUIPE
from app.agente.llm import historico


@pytest.fixture
def usar(monkeypatch):
    def _usar(provedor: str, **valores):
        monkeypatch.setattr(config, "LLM_PROVIDER", provedor)
        for nome, valor in valores.items():
            monkeypatch.setattr(config, nome, valor)
    return _usar


@pytest.mark.parametrize("provedor, classe, chave", [
    ("gemini", "ChatGoogleGenerativeAI", {"GEMINI_API_KEY": "teste"}),
    ("anthropic", "ChatAnthropic", {"ANTHROPIC_API_KEY": "teste"}),
    ("claude", "ChatAnthropic", {"ANTHROPIC_API_KEY": "teste"}),
    ("openai", "ChatOpenAI", {"OPENAI_API_KEY": "teste"}),
    ("ollama", "ChatOllama", {}),
])
def test_cada_provedor_monta_seu_modelo_e_aceita_as_intencoes(usar, provedor, classe, chave):
    usar(provedor, **chave)
    modelo = provedores.criar_modelo()
    assert type(modelo).__name__ == classe
    # as intenções viram ferramentas no formato de cada provedor (falha aqui = schema que o provedor não aceita)
    for ferramentas in (FERRAMENTAS, FERRAMENTAS_EQUIPE):
        assert modelo.bind_tools(ferramentas, tool_choice="any") is not None


def test_gemini_e_o_padrao_e_usa_a_chave_e_o_modelo_do_env(usar):
    usar("gemini", GEMINI_API_KEY="teste", GEMINI_MODEL="gemini-x")
    p = provedores.atual()
    assert p.rotulo == "Gemini" and p.aceita_audio and provedores.configurado()
    assert provedores.criar_modelo().model.endswith("gemini-x")
    assert provedores.descricao() == "Gemini (gemini-x)"


def test_modelo_configurado_chega_ao_provedor(usar):
    usar("anthropic", ANTHROPIC_API_KEY="teste", ANTHROPIC_MODEL="claude-sonnet-5-5")
    assert provedores.criar_modelo().model == "claude-sonnet-5-5"
    assert not provedores.aceita_audio()


def test_openai_aceita_api_compativel(usar):
    usar("openai", OPENAI_API_KEY="teste", OPENAI_BASE_URL="https://openrouter.ai/api/v1")
    assert provedores.criar_modelo().openai_api_base == "https://openrouter.ai/api/v1"
    usar("openai", OPENAI_API_KEY="teste", OPENAI_BASE_URL=None)
    assert provedores.criar_modelo().openai_api_base == provedores.OPENAI_URL_PADRAO


def test_sem_chave_diz_qual_variavel_preencher(usar):
    usar("anthropic", ANTHROPIC_API_KEY=None)
    assert not provedores.configurado()
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        provedores.criar_modelo()


def test_ollama_nao_precisa_de_chave(usar):
    usar("ollama")
    assert provedores.configurado()


def test_provedor_desconhecido_lista_as_opcoes(usar):
    usar("mistral")
    assert not provedores.configurado() and not provedores.aceita_audio()
    with pytest.raises(RuntimeError, match="gemini, anthropic, openai, ollama"):
        provedores.criar_modelo()
    assert "LLM_PROVIDER='mistral'" in provedores.explicar_erro(RuntimeError("x"))


def test_mensagens_de_erro_citam_o_provedor_e_a_variavel(usar):
    usar("gemini")
    assert "SEM faturamento" in provedores.explicar_erro(RuntimeError("402 Your prepayment credits are depleted"))
    assert "limite gratuito do Gemini" in provedores.explicar_erro(RuntimeError("429 RESOURCE_EXHAUSTED"))
    assert "GEMINI_API_KEY" in provedores.explicar_erro(RuntimeError("400 API key not valid. API_KEY_INVALID"))

    usar("anthropic", ANTHROPIC_MODEL="claude-x")
    sem_credito = provedores.explicar_erro(RuntimeError("400 Your credit balance is too low to access the API"))
    assert "Claude está sem créditos" in sem_credito and "ANTHROPIC_API_KEY" in sem_credito
    assert "ANTHROPIC_API_KEY" in provedores.explicar_erro(RuntimeError("401 authentication_error: invalid x-api-key"))
    assert "ANTHROPIC_MODEL" in provedores.explicar_erro(RuntimeError("404 not_found_error: model: claude-x"))
    assert "sobrecarregado" in provedores.explicar_erro(RuntimeError("529 overloaded_error"))

    usar("openai")
    assert "sem créditos" in provedores.explicar_erro(RuntimeError("429 insufficient_quota"))
    assert "limite de uso" in provedores.explicar_erro(RuntimeError("429 Rate limit reached"))

    usar("ollama", OLLAMA_MODEL="qwen3")
    assert "Ollama em" in provedores.explicar_erro(ConnectionError("Connection refused"))
    assert "ollama pull qwen3" in provedores.explicar_erro(RuntimeError("model 'qwen3' not found (status code: 404)"))


def test_historico_comeca_pelo_usuario():
    msgs = [AIMessage("a0"), HumanMessage("h1"), AIMessage("a1"), HumanMessage("h2")]
    assert [m.content for m in historico(msgs)] == ["h1", "a1", "h2"]
    assert [m.content for m in historico(msgs, n=3)] == ["h1", "a1", "h2"]
    assert [m.content for m in historico(msgs, n=2)] == ["h2"]
