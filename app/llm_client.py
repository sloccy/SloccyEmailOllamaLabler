import os
import requests

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")


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
                timeout=600,
            )
            print(f"Model {OLLAMA_MODEL} ready.")
    except Exception as e:
        print(f"Warning: could not check/pull Ollama model: {e}")


def should_apply_label(email: dict, instructions: str) -> bool:
    prompt = f"""You are an email classification assistant. Your only job is to decide whether to apply a label to an email based on a rule.

Rule: {instructions}

Email:
From: {email['sender']}
Subject: {email['subject']}
Body:
{email['body'] or email['snippet']}

Based on the rule above, should this email be labeled?
Reply with only one word: YES or NO."""

    response = requests.post(
        f"{OLLAMA_HOST}/api/generate",
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0, "num_predict": 5},
        },
        timeout=600,
    )
    response.raise_for_status()
    answer = response.json().get("response", "").strip().upper()
    return answer.startswith("YES")
