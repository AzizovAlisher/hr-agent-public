from functools import lru_cache

from app.config import get_settings
from app.llm.base import (
    ChatMessage,
    CompletionResult,
    LLMClient,
    LogContext,
    StreamCompletionEvent,
)
from app.llm.gateway import (
    BudgetedLLMClient,
    LLMConfigurationError,
    LLMModelBudgetExceeded,
    LLMProtocolError,
)
from app.llm.openrouter import OpenRouterClient
from app.llm.vllm import VLLMClient


@lru_cache
def get_llm_client() -> LLMClient:
    provider = get_settings().llm_provider.lower()
    if provider == "openrouter":
        return OpenRouterClient()
    if provider == "vllm":
        return VLLMClient()
    raise ValueError(f"Unknown LLM_PROVIDER: {provider}")


__all__ = [
    "BudgetedLLMClient",
    "ChatMessage",
    "CompletionResult",
    "LLMClient",
    "LLMConfigurationError",
    "LLMModelBudgetExceeded",
    "LLMProtocolError",
    "LogContext",
    "StreamCompletionEvent",
    "get_llm_client",
]
