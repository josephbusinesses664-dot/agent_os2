import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from agentos.config import Settings
from agentos.domain.models import ModelDef, ModelRequest
from agentos.models.openai_compat import OpenAICompatProvider
from agentos.models.anthropic import AnthropicProvider
import json

@pytest.fixture
def settings():
    return Settings(
        anthropic_api_key="test-anthropic-key",
        anthropic_base_url="https://test-anthropic",
        openai_compat_api_key="test-openai-key",
        openai_compat_base_url="https://test-openai",
    )

@pytest.fixture
def model_defs():
    return {
        "gpt-4": ModelDef(
            id="gpt-4",
            provider="openai_compat",
            name="gpt-4-turbo",
            price_in_per_million=10.0,
            price_out_per_million=30.0,
        ),
        "claude-3": ModelDef(
            id="claude-3",
            provider="anthropic",
            name="claude-3-opus",
            price_in_per_million=15.0,
            price_out_per_million=75.0,
        ),
    }


@pytest.mark.asyncio
async def test_openai_compat_happy_path(settings, model_defs):
    provider = OpenAICompatProvider(
        settings, model_defs, "test_openai", "https://test-openai", "test-key"
    )
    request = ModelRequest(
        model_id="gpt-4",
        messages=[{"role": "user", "content": "Hello"}],
        system="System prompt",
        temperature=0.5,
        max_tokens=100,
        tools=[{"type": "function", "function": {"name": "test_func"}}],
    )

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": "Hi there",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {"name": "test_func", "arguments": '{"arg": 1}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp

    with patch("httpx.AsyncClient", return_value=mock_client) as MockClient:
        MockClient.return_value.__aenter__.return_value = mock_client
        response = await provider.complete(request)

    assert response.model_id == "gpt-4"
    assert response.content == "Hi there"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0]["name"] == "test_func"
    assert response.tool_calls[0]["arguments"] == '{"arg": 1}'
    assert response.prompt_tokens == 10
    assert response.completion_tokens == 20
    assert response.total_tokens == 30
    assert response.estimated_cost == (10 / 1_000_000 * 10.0) + (20 / 1_000_000 * 30.0)
    assert response.finish_reason == "tool_calls"
    assert response.error is None

    mock_client.post.assert_called_once()
    args, kwargs = mock_client.post.call_args
    assert args[0] == "https://test-openai/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer test-key"
    assert kwargs["json"]["model"] == "gpt-4-turbo"
    assert kwargs["json"]["messages"][0] == {"role": "system", "content": "System prompt"}
    assert kwargs["json"]["messages"][1] == {"role": "user", "content": "Hello"}
    assert kwargs["json"]["tools"] == request.tools
    assert kwargs["json"]["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_openai_compat_unknown_model(settings, model_defs):
    provider = OpenAICompatProvider(
        settings, model_defs, "test_openai", "https://test-openai", "test-key"
    )
    request = ModelRequest(model_id="unknown")
    response = await provider.complete(request)
    assert response.error == "unknown model unknown"


@pytest.mark.asyncio
async def test_openai_compat_not_configured(settings, model_defs):
    provider = OpenAICompatProvider(
        settings, model_defs, "test_openai", "", ""
    )
    request = ModelRequest(model_id="gpt-4")
    response = await provider.complete(request)
    assert "not configured" in response.error


@pytest.mark.asyncio
async def test_openai_compat_error(settings, model_defs):
    provider = OpenAICompatProvider(
        settings, model_defs, "test_openai", "https://test-openai", "test-key"
    )
    request = ModelRequest(model_id="gpt-4")

    mock_client = AsyncMock()
    mock_client.post.side_effect = Exception("Network error")

    with patch("httpx.AsyncClient", return_value=mock_client) as MockClient:
        MockClient.return_value.__aenter__.return_value = mock_client
        response = await provider.complete(request)

    assert response.error == "test_openai request failed: Network error"


@pytest.mark.asyncio
async def test_anthropic_happy_path(settings, model_defs):
    provider = AnthropicProvider(settings, model_defs)
    request = ModelRequest(
        model_id="claude-3",
        messages=[{"role": "user", "content": "Hello"}, {"role": "system", "content": "Ignore this as it should be filtered or put top level"}],
        system="System prompt",
        max_tokens=100,
    )

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "content": [
            {"type": "text", "text": "Hi there"},
            {"type": "tool_use", "id": "call_1", "name": "test_func", "input": {"arg": 1}},
        ],
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "stop_reason": "tool_use",
    }

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp

    with patch("httpx.AsyncClient", return_value=mock_client) as MockClient:
        MockClient.return_value.__aenter__.return_value = mock_client
        response = await provider.complete(request)

    assert response.model_id == "claude-3"
    assert response.content == "Hi there"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0]["name"] == "test_func"
    assert response.tool_calls[0]["arguments"] == '{"arg": 1}'
    assert response.prompt_tokens == 10
    assert response.completion_tokens == 20
    assert response.total_tokens == 30
    assert response.estimated_cost == (10 / 1_000_000 * 15.0) + (20 / 1_000_000 * 75.0)
    assert response.finish_reason == "tool_use"
    assert response.error is None

    mock_client.post.assert_called_once()
    args, kwargs = mock_client.post.call_args
    assert args[0] == "https://test-anthropic/v1/messages"
    assert kwargs["headers"]["x-api-key"] == "test-anthropic-key"
    assert kwargs["json"]["model"] == "claude-3-opus"
    assert kwargs["json"]["system"] == "System prompt"
    # System role from messages array should be filtered out
    assert len(kwargs["json"]["messages"]) == 1
    assert kwargs["json"]["messages"][0] == {"role": "user", "content": "Hello"}


@pytest.mark.asyncio
async def test_anthropic_empty_messages(settings, model_defs):
    provider = AnthropicProvider(settings, model_defs)
    request = ModelRequest(model_id="claude-3", messages=[])

    mock_resp = MagicMock()
    mock_resp.json.return_value = {}
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp

    with patch("httpx.AsyncClient", return_value=mock_client) as MockClient:
        MockClient.return_value.__aenter__.return_value = mock_client
        await provider.complete(request)

    _, kwargs = mock_client.post.call_args
    assert kwargs["json"]["messages"] == [{"role": "user", "content": "(empty request)"}]


@pytest.mark.asyncio
async def test_anthropic_unknown_model(settings, model_defs):
    provider = AnthropicProvider(settings, model_defs)
    request = ModelRequest(model_id="unknown")
    response = await provider.complete(request)
    assert response.error == "unknown model unknown"


@pytest.mark.asyncio
async def test_anthropic_not_configured(settings, model_defs):
    settings.anthropic_api_key = ""
    provider = AnthropicProvider(settings, model_defs)
    request = ModelRequest(model_id="claude-3")
    response = await provider.complete(request)
    assert "not configured" in response.error


@pytest.mark.asyncio
async def test_anthropic_error(settings, model_defs):
    provider = AnthropicProvider(settings, model_defs)
    request = ModelRequest(model_id="claude-3")

    mock_client = AsyncMock()
    mock_client.post.side_effect = Exception("Network error")

    with patch("httpx.AsyncClient", return_value=mock_client) as MockClient:
        MockClient.return_value.__aenter__.return_value = mock_client
        response = await provider.complete(request)

    assert response.error == "anthropic request failed: Network error"
