from app.llm.base import LLMProvider
from app.llm.ollama import OllamaProvider

_provider: LLMProvider | None = None


def get_provider() -> LLMProvider:
    global _provider
    if _provider is None:
        _provider = OllamaProvider()
    return _provider
