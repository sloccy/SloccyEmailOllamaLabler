"""
Compatibility shim — delegates to app.llm.OllamaProvider.
Existing callers (poller.py, server.py) continue to work unchanged.
To swap LLM providers, update app/llm/__init__.py:get_provider().
"""
from app.llm import get_provider

_provider = get_provider()


def ensure_model_pulled():
    _provider.ensure_model_pulled()


def classify_email_batch(email: dict, prompts: list) -> dict:
    return _provider.classify_email_batch(email, prompts)


def generate_prompt_instruction(description: str) -> str:
    return _provider.generate_prompt_instruction(description)


def stream_generate_prompt_instruction(description: str):
    return _provider.stream_generate_prompt_instruction(description)
