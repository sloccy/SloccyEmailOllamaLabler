import json
import re

import ollama as _ollama

from app import db
from app.config import (
    DEBUG_LOGGING,
    OLLAMA_GENERATE_NUM_PREDICT,
    OLLAMA_HOST,
    OLLAMA_MODEL,
    OLLAMA_NUM_CTX,
    OLLAMA_TIMEOUT,
)

_client = _ollama.Client(host=OLLAMA_HOST, timeout=OLLAMA_TIMEOUT)


class LLMError(Exception):
    """Raised when the LLM fails to produce a usable classification."""


def ensure_model_pulled() -> None:
    try:
        models = [m.model for m in _client.list().models]
        model_base = OLLAMA_MODEL.split(":")[0]
        if not any(m.startswith(model_base) for m in models):
            db.add_log("INFO", f"Pulling model {OLLAMA_MODEL} from Ollama... (this may take a while)")
            _client.pull(OLLAMA_MODEL)
            db.add_log("INFO", f"Model {OLLAMA_MODEL} ready.")
    except Exception as e:
        db.add_log("WARNING", f"Could not check/pull Ollama model: {e}")


def classify_email_batch(email: dict, prompts: list) -> dict:
    if not prompts:
        return {}

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
        raw = response.message.content.strip()
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
        return parsed
    except json.JSONDecodeError as e:
        db.add_log("ERROR", f"LLM parse error: {e!r} | raw: {raw!r}")
        raise LLMError(f"LLM parse error: {e!r}") from e
    except Exception as e:
        db.add_log("ERROR", f"LLM request failed: {e!r}")
        raise LLMError(f"LLM request failed: {e!r}") from e


def _filter_think_chunks(buffer: str, in_think: bool, chunk: str):
    """Append chunk to buffer, flush safe content, return (events, new_buffer, in_think).
    Events are (type, text) tuples where type is 'think' or 'content'."""
    buffer += chunk
    events = []
    while True:
        tag = "</think>" if in_think else "<think>"
        idx = buffer.find(tag)
        if idx == -1:
            safe = max(0, len(buffer) - (len(tag) - 1))
            if safe > 0:
                events.append(("think" if in_think else "content", buffer[:safe]))
                buffer = buffer[safe:]
            break
        else:
            if idx > 0:
                events.append(("think" if in_think else "content", buffer[:idx]))
            buffer = buffer[idx + len(tag) :]
            in_think = not in_think
    return events, buffer, in_think


def stream_generate_prompt_instruction(description: str):
    """Generator that yields {"type": "think"|"content", "text": str} dicts."""
    in_think = False
    buffer = ""
    for chunk in _client.chat(
        model=OLLAMA_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You write email filter rules for an AI classifier. "
                    "The classifier reads email content and infers meaning, intent, and context — "
                    "it is NOT limited to keywords or sender addresses. "
                    "Rules should describe what an email is about, its purpose, and tone. "
                    "Be specific about what should match and what should not.\n"
                    "Output only the rule text. No preamble, no quotes, no explanation."
                ),
            },
            {
                "role": "user",
                "content": (
                    f'A user wants to automatically label certain emails. They described:\n\n"{description}"\n\n'
                    "Write a precise classifier instruction (2-5 sentences). "
                    "Focus on the meaning and context of the email — what it is about, why it was sent, "
                    "and who it is intended for. Describe what distinguishes matching emails from "
                    "similar-but-different ones based on content and intent, not just surface signals "
                    "like sender address or keywords.\n\n"
                    "Respond with ONLY the instruction text."
                ),
            },
        ],
        stream=True,
        options={
            "temperature": 0.7,
            "num_predict": OLLAMA_GENERATE_NUM_PREDICT,
            "num_ctx": OLLAMA_NUM_CTX,
        },
    ):
        token = chunk.message.content
        if not token:
            continue
        events, buffer, in_think = _filter_think_chunks(buffer, in_think, token)
        for evt_type, evt_text in events:
            if evt_text:
                yield {"type": evt_type, "text": evt_text}
    if buffer:
        yield {"type": "think" if in_think else "content", "text": buffer}
