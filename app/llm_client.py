import os
import json
import requests
from app import db
from app.config import OLLAMA_HOST, OLLAMA_MODEL, OLLAMA_TIMEOUT, OLLAMA_NUM_CTX, OLLAMA_NUM_PREDICT


def ensure_model_pulled():
    try:
        resp = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=10)
        models = [m["name"] for m in resp.json().get("models", [])]
        model_base = OLLAMA_MODEL.split(":")[0]
        already_present = any(m.startswith(model_base) for m in models)
        if not already_present:
            print(f"Pulling model {OLLAMA_MODEL} from Ollama... (this may take a while)")
            requests.post(
                f"{OLLAMA_HOST}/api/pull",
                json={"name": OLLAMA_MODEL, "stream": False},
                timeout=OLLAMA_TIMEOUT,
            )
            print(f"Model {OLLAMA_MODEL} ready.")
    except Exception as e:
        print(f"Warning: could not check/pull Ollama model: {e}")


def classify_email_batch(email: dict, prompts: list) -> dict:
    """
    Classify an email against all prompts in a single LLM call.
    Returns a dict mapping prompt id (int) -> bool.
    """
    if not prompts:
        return {}

    rules_text = "\n".join(
        f"{i+1}. [id:{p['id']}] {p['name']}: {p['instructions']}"
        for i, p in enumerate(prompts)
    )

    example = ", ".join(f'"{p["id"]}": false' for p in prompts[:2])
    prompt = f"""You are an email classification assistant. You will be given an email and a list of labeling rules. For each rule, decide if the label should be applied to this email.

Rules:
{rules_text}

Email:
From: {email['sender']}
Subject: {email['subject']}
Body:
{email['body'] or email['snippet']}

Respond with ONLY a JSON object where each key is the rule's [id] number and the value is true or false.
Example: {{{example}}}
No explanation, no markdown, just the JSON object."""

    try:
        response = requests.post(
            f"{OLLAMA_HOST}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are an email classification assistant. Respond only with a JSON object mapping rule IDs to true/false. No explanation, no markdown.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "think": False,
                "format": "json",
                "options": {
                    "temperature": 0,
                    "num_predict": OLLAMA_NUM_PREDICT,
                    "num_ctx": OLLAMA_NUM_CTX,
                },
            },
            timeout=OLLAMA_TIMEOUT,
        )
        response.raise_for_status()

        raw = response.json().get("message", {}).get("content", "").strip()

        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        try:
            result = json.loads(raw)
            parsed = {int(k): bool(v) for k, v in result.items()}
            db.add_log("DEBUG", f"LLM raw response: {raw}")
            db.add_log("DEBUG", f"LLM parsed: { {p['name']: parsed.get(p['id'], False) for p in prompts} }")
            return parsed
        except Exception as e:
            db.add_log("ERROR", f"LLM parse error: {e!r} | raw: {raw!r}")
            print(f"Warning: could not parse LLM batch response: {e!r} | raw: {raw!r}")
            return {p["id"]: False for p in prompts}
    except requests.exceptions.RequestException as e:
        db.add_log("ERROR", f"LLM request failed: {e!r}")
        print(f"Warning: LLM request failed: {e!r}")
        # Return default values to avoid stopping the entire process
        return {p["id"]: False for p in prompts}
    except Exception as e:
        db.add_log("ERROR", f"LLM unexpected error: {e!r}")
        print(f"Warning: LLM unexpected error: {e!r}")
        # Return default values to avoid stopping the entire process
        return {p["id"]: False for p in prompts}
