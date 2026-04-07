import json
import logging
import re

import ollama as _ollama
from ollama import ResponseError as _ResponseError

from app import db
from app.config import (
    DEBUG_LOGGING,
    OLLAMA_GENERATE_NUM_PREDICT,
    OLLAMA_HOST,
    OLLAMA_MODEL,
    OLLAMA_NUM_CTX,
    OLLAMA_TIMEOUT,
)

_logger = logging.getLogger("ollamail.llm")
_client = _ollama.Client(host=OLLAMA_HOST, timeout=OLLAMA_TIMEOUT)

# Safety net: strip <think> tags in case some ollama versions embed them in content
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMError(Exception):
    """Raised when the LLM fails to produce a usable classification."""


def ensure_model_pulled() -> None:
    try:
        _client.show(OLLAMA_MODEL)
    except _ResponseError:
        db.add_log("INFO", f"Pulling model {OLLAMA_MODEL} from Ollama... (this may take a while)")
        _client.pull(OLLAMA_MODEL)
        db.add_log("INFO", f"Model {OLLAMA_MODEL} ready.")
    except Exception as e:
        db.add_log("WARNING", f"Could not check/pull Ollama model: {e}")


def classify_email_batch(email: dict, prompts: list) -> tuple:
    """Returns (parsed_results: dict[int, bool], raw_response: str)."""
    if not prompts:
        return {}, ""

    rules_text = "\n".join(f"{i + 1}. {p['name']}: {p['instructions']}" for i, p in enumerate(prompts))
    example = ", ".join(f'"{i + 1}": false' for i in range(min(2, len(prompts))))
    prompt = f"""You are an email classification assistant. You will be given an email and a list of labeling rules. For each rule, decide if the label should be applied to this email.

Rules:
{rules_text}

Email:
From: {email["sender"]}
Subject: {email["subject"]}
Body:
{email["body"] or email["snippet"]}

Respond with ONLY a JSON object where each key is the rule's number (1, 2, 3...) and the value is true or false.
Example: {{{example}}}
No explanation, no markdown, just the JSON object."""

    db.add_log("INFO", f"LLM classifying '{email.get('subject', '?')[:60]}' against {len(prompts)} rule(s)")
    try:
        response = _client.chat(
            model=OLLAMA_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "You are an email classification assistant. Respond only with a JSON object mapping rule numbers to true/false. No explanation, no markdown.",
                },
                {"role": "user", "content": prompt},
            ],
            think=False,
            format="json",
            options={
                "temperature": 0,
                "num_predict": max(50, len(prompts) * 20),
                "num_ctx": OLLAMA_NUM_CTX,
            },
        )
        raw = response.message.content or ""
        thinking = getattr(response.message, "thinking", None) or ""
        db.add_log("INFO", f"LLM classify response: content={len(raw)} chars, thinking={len(thinking)} chars")
        if raw:
            db.add_log("INFO", f"LLM raw content: {raw[:500]}")
        if thinking:
            db.add_log("INFO", f"LLM thinking (first 200): {thinking[:200]}")
        # If think=False didn't suppress thinking and content is empty, fall back to thinking field
        if not raw.strip() and thinking.strip():
            db.add_log("WARNING", "LLM returned empty content with think=False — falling back to thinking field")
            raw = thinking
        raw_response = raw  # save original for history before stripping
        raw = _THINK_RE.sub("", raw).strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
        result = json.loads(raw)
        parsed = {}
        for k, v in result.items():
            idx = int(k) - 1
            if 0 <= idx < len(prompts):
                parsed[prompts[idx]["id"]] = bool(v)
        if DEBUG_LOGGING:
            db.add_log("DEBUG", f"LLM raw response: {raw}")
            db.add_log("DEBUG", f"LLM parsed: { {p['name']: parsed.get(p['id'], False) for p in prompts} }")
        return parsed, raw_response
    except json.JSONDecodeError as e:
        db.add_log("ERROR", f"LLM parse error: {e!r} | raw: {raw!r}")
        raise LLMError(f"LLM parse error: {e!r}") from e
    except Exception as e:
        db.add_log("ERROR", f"LLM request failed: {e!r}")
        raise LLMError(f"LLM request failed: {e!r}") from e


def stream_generate_prompt_instruction(description: str):
    """Generator that yields {"type": "think"|"content", "text": str} dicts."""
    for chunk in _client.chat(
        model=OLLAMA_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You write email filter rules for an AI classifier. "
                    "Output only the rule text. No preamble, no drafts, no self-critique, no quotes, no explanation."
                ),
            },
            {
                "role": "user",
                "content": (
                    f'Write a 2-4 sentence classifier instruction for emails matching: "{description}"\n\n'
                    "The instruction must describe: what the email is about, its purpose/intent, "
                    "and what distinguishes it from similar-but-non-matching emails. "
                    "Do not use keywords or sender addresses as criteria — focus on meaning and context.\n\n"
                    "Output ONLY the instruction text."
                ),
            },
        ],
        stream=True,
        think=True,
        options={
            "temperature": 0.7,
            "num_predict": OLLAMA_GENERATE_NUM_PREDICT,
            "num_ctx": OLLAMA_NUM_CTX,
        },
    ):
        thinking = getattr(chunk.message, "thinking", None)
        content = chunk.message.content or ""
        if thinking:
            yield {"type": "think", "text": thinking}
        if content:
            yield {"type": "content", "text": content}
    _logger.info("stream_generate_prompt_instruction finished")
