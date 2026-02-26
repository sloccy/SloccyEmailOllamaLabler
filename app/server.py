import os
import json
import secrets
from urllib.parse import urlparse, parse_qs
from flask import Flask, jsonify, request, render_template, session, Response
from app import db, gmail_client, poller, llm_client

app = Flask(__name__, template_folder="templates")
app.secret_key = "placeholder-replaced-at-startup"


# ---- UI ----

@app.route("/")
def index():
    return render_template("index.html")


# ---- OAuth ----

@app.route("/api/oauth/start", methods=["POST"])
def oauth_start():
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    try:
        auth_url = gmail_client.get_auth_url(state)
        return jsonify({"auth_url": auth_url, "state": state})
    except FileNotFoundError:
        return jsonify({"error": "credentials.json not found. Place your Google OAuth credentials file at /credentials/credentials.json."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/oauth/exchange", methods=["POST"])
def oauth_exchange():
    data = request.json
    pasted_url = data.get("url", "").strip()
    if not pasted_url:
        return jsonify({"error": "No URL provided."}), 400
    try:
        parsed = urlparse(pasted_url)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
    except Exception:
        return jsonify({"error": "Could not parse the URL."}), 400
    if not code:
        return jsonify({"error": "No authorization code found in the URL."}), 400
    expected_state = session.get("oauth_state")
    if not expected_state or state != expected_state:
        return jsonify({"error": "State mismatch. Please start the authorization process again."}), 400
    try:
        email, credentials_json = gmail_client.exchange_code(state, code)
        db.upsert_account(email, credentials_json)
        db.add_log("INFO", f"Account connected: {email}")
        return jsonify({"ok": True, "email": email})
    except Exception as e:
        db.add_log("ERROR", f"OAuth exchange failed: {e}")
        return jsonify({"error": str(e)}), 500


# ---- Accounts ----

@app.route("/api/accounts", methods=["GET"])
def api_list_accounts():
    accounts = db.list_accounts()
    safe = [{k: v for k, v in a.items() if k != "credentials_json"} for a in accounts]
    return jsonify(safe)


@app.route("/api/accounts/<int:account_id>", methods=["DELETE"])
def api_delete_account(account_id):
    db.delete_account(account_id)
    db.add_log("INFO", f"Account {account_id} removed.")
    return jsonify({"ok": True})


@app.route("/api/accounts/<int:account_id>/toggle", methods=["POST"])
def api_toggle_account(account_id):
    account = db.get_account(account_id)
    if not account:
        return jsonify({"error": "Not found"}), 404
    new_state = 0 if account["active"] else 1
    with db.get_db() as conn:
        conn.execute("UPDATE accounts SET active=? WHERE id=?", (new_state, account_id))
    return jsonify({"active": new_state})


# ---- Prompts ----

@app.route("/api/prompts", methods=["GET"])
def api_list_prompts():
    # Optional filter: ?account_id=1 returns prompts for that account + global prompts
    # No param returns all prompts (for the management UI)
    account_id = request.args.get("account_id")
    if account_id:
        prompts = db.list_prompts(account_id=int(account_id))
    else:
        prompts = db.list_prompts()
    return jsonify(prompts)


@app.route("/api/prompts", methods=["POST"])
def api_create_prompt():
    data = request.json
    if not data.get("name") or not data.get("instructions") or not data.get("label_name"):
        return jsonify({"error": "name, instructions, and label_name are required"}), 400
    account_id = data.get("account_id")
    db.create_prompt(
        data["name"],
        data["instructions"],
        data["label_name"],
        action_archive=int(data.get("action_archive", 0)),
        action_spam=int(data.get("action_spam", 0)),
        action_move_to=data.get("action_move_to", "").strip(),
        stop_processing=int(data.get("stop_processing", 0)),
        account_id=int(account_id) if account_id else None,
    )
    scope = f"account {account_id}" if account_id else "all accounts"
    db.add_log("INFO", f"Prompt created: {data['name']} → label '{data['label_name']}' ({scope})")
    return jsonify({"ok": True}), 201


@app.route("/api/prompts/<int:prompt_id>", methods=["PUT"])
def api_update_prompt(prompt_id):
    data = request.json
    account_id = data.get("account_id")
    db.update_prompt(
        prompt_id,
        data["name"],
        data["instructions"],
        data["label_name"],
        int(data.get("active", 1)),
        action_archive=int(data.get("action_archive", 0)),
        action_spam=int(data.get("action_spam", 0)),
        action_move_to=data.get("action_move_to", "").strip(),
        stop_processing=int(data.get("stop_processing", 0)),
        account_id=int(account_id) if account_id else None,
    )
    return jsonify({"ok": True})


@app.route("/api/prompts/<int:prompt_id>", methods=["DELETE"])
def api_delete_prompt(prompt_id):
    db.delete_prompt(prompt_id)
    return jsonify({"ok": True})


@app.route("/api/prompts/reorder", methods=["POST"])
def api_reorder_prompts():
    data = request.json
    ordered_ids = data.get("ordered_ids", [])
    if not ordered_ids:
        return jsonify({"error": "ordered_ids required"}), 400
    db.reorder_prompts([int(i) for i in ordered_ids])
    return jsonify({"ok": True})


@app.route("/api/prompts/export", methods=["GET"])
def api_export_prompts():
    """
    Export prompts as a JSON file download.
    Optional ?account_id=X to export only prompts for that account + globals.
    Optional ?account_id=X&name=email@example.com to name the file nicely.
    """
    account_id = request.args.get("account_id")
    account_name = request.args.get("name", "all")

    if account_id:
        prompts = db.list_prompts(account_id=int(account_id))
    else:
        prompts = db.list_prompts()

    # Strip internal DB fields not useful for export
    export_fields = ["name", "instructions", "label_name", "active", "action_archive",
                     "action_spam", "action_move_to", "stop_processing", "account_id"]
    export_data = [{k: p[k] for k in export_fields if k in p} for p in prompts]

    # Resolve account_id to email for readability
    accounts = {a["id"]: a["email"] for a in db.list_accounts()}
    for p in export_data:
        aid = p.get("account_id")
        p["account"] = accounts.get(aid, "all accounts") if aid else "all accounts"
        del p["account_id"]

    filename = f"prompts-{account_name.replace('@', '_').replace('.', '_')}.json"
    return Response(
        json.dumps(export_data, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---- Settings ----

@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    return jsonify({
        "poll_interval": int(db.get_setting("poll_interval", "300")),
        "ollama_model": os.getenv("OLLAMA_MODEL", "llama3.2"),
        "ollama_host": os.getenv("OLLAMA_HOST", "http://localhost:11434"),
        "ollama_timeout": int(os.getenv("OLLAMA_TIMEOUT", "600")),
        "ollama_num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "4096")),
        "ollama_num_predict": int(os.getenv("OLLAMA_NUM_PREDICT", "200")),
        "gmail_max_results": int(os.getenv("GMAIL_MAX_RESULTS", "50")),
        "gmail_lookback_hours": int(os.getenv("GMAIL_LOOKBACK_HOURS", "24")),
    })


@app.route("/api/settings", methods=["POST"])
def api_update_settings():
    data = request.json
    if "poll_interval" in data:
        val = int(data["poll_interval"])
        if val < 30:
            return jsonify({"error": "Minimum poll interval is 30 seconds"}), 400
        db.set_setting("poll_interval", str(val))
    db.add_log("INFO", f"Settings updated: poll_interval={data.get('poll_interval')}s")
    return jsonify({"ok": True})


# ---- Logs + Status ----

@app.route("/api/logs", methods=["GET"])
def api_get_logs():
    limit = int(request.args.get("limit", 100))
    return jsonify(db.get_logs(limit))


@app.route("/api/status", methods=["GET"])
def api_status():
    import time
    return jsonify({**poller.get_status(), "current_time": time.time()})


@app.route("/api/scan", methods=["POST"])
def api_scan_now():
    db.add_log("INFO", "Manual scan triggered.")
    poller.run_now()
    return jsonify({"ok": True})


# ---- Startup ----

def _get_or_create_secret_key() -> str:
    key = db.get_setting("flask_secret_key")
    if not key:
        key = secrets.token_hex(32)
        db.set_setting("flask_secret_key", key)
    return key


def create_app():
    db.init_db()
    app.secret_key = _get_or_create_secret_key()
    import threading
    threading.Thread(target=llm_client.ensure_model_pulled, daemon=True).start()
    poller.start()
    return app


if __name__ == "__main__":
    application = create_app()
    application.run(host="0.0.0.0", port=5000, debug=False)
