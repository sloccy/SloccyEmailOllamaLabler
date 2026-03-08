import json
import secrets
from urllib.parse import urlparse, parse_qs
from flask import Flask, jsonify, request, render_template, session, Response
from app import db, gmail_client, poller, llm_client
from app.config import (POLL_INTERVAL, OLLAMA_MODEL, OLLAMA_HOST, OLLAMA_TIMEOUT,
                        OLLAMA_NUM_CTX, OLLAMA_NUM_PREDICT, GMAIL_MAX_RESULTS,
                        GMAIL_LOOKBACK_HOURS, MIN_POLL_INTERVAL, HISTORY_MAX_LIMIT)

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
    if data is None:
        return jsonify({"error": "JSON body required."}), 400
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
    new_state = db.toggle_account(account_id)
    if new_state is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"active": new_state})


def _ensure_label_for_accounts(account_id, label_name):
    if account_id is not None:
        accounts = [db.get_account(account_id)]
    else:
        accounts = [a for a in db.list_accounts() if a.get("active")]
    for account in accounts:
        if not account:
            continue
        try:
            service, refreshed_creds = gmail_client.get_service(account["credentials_json"])
            if json.loads(refreshed_creds) != json.loads(account["credentials_json"]):
                db.update_account_credentials(account["id"], refreshed_creds)
            gmail_client.build_label_cache(service, [label_name])
        except Exception as e:
            db.add_log("WARNING", f"Could not pre-create label '{label_name}' for account {account.get('id')}: {e}")


# ---- Prompts ----

@app.route("/api/prompts", methods=["GET"])
def api_list_prompts():
    account_id = request.args.get("account_id")
    if account_id:
        prompts = db.list_prompts(account_id=int(account_id))
    else:
        prompts = db.list_prompts()
    return jsonify(prompts)


@app.route("/api/prompts", methods=["POST"])
def api_create_prompt():
    data = request.json
    if data is None:
        return jsonify({"error": "JSON body required."}), 400
    if not data.get("name") or not data.get("instructions") or not data.get("label_name"):
        return jsonify({"error": "name, instructions, and label_name are required"}), 400
    account_id = data.get("account_id")
    db.create_prompt(
        data["name"],
        data["instructions"],
        data["label_name"],
        action_archive=int(data.get("action_archive", 0)),
        action_spam=int(data.get("action_spam", 0)),
        action_trash=int(data.get("action_trash", 0)),
        action_mark_read=int(data.get("action_mark_read", 0)),
        stop_processing=int(data.get("stop_processing", 0)),
        account_id=int(account_id) if account_id else None,
    )
    _ensure_label_for_accounts(int(account_id) if account_id else None, data["label_name"])
    scope = f"account {account_id}" if account_id else "all accounts"
    db.add_log("INFO", f"Prompt created: {data['name']} → label '{data['label_name']}' ({scope})")
    return jsonify({"ok": True}), 201


@app.route("/api/prompts/<int:prompt_id>", methods=["PUT"])
def api_update_prompt(prompt_id):
    data = request.json
    if data is None:
        return jsonify({"error": "JSON body required."}), 400
    account_id = data.get("account_id")
    db.update_prompt(
        prompt_id,
        data["name"],
        data["instructions"],
        data["label_name"],
        int(data.get("active", 1)),
        action_archive=int(data.get("action_archive", 0)),
        action_spam=int(data.get("action_spam", 0)),
        action_trash=int(data.get("action_trash", 0)),
        action_mark_read=int(data.get("action_mark_read", 0)),
        stop_processing=int(data.get("stop_processing", 0)),
        account_id=int(account_id) if account_id else None,
    )
    _ensure_label_for_accounts(int(account_id) if account_id else None, data["label_name"])
    return jsonify({"ok": True})


@app.route("/api/prompts/<int:prompt_id>", methods=["DELETE"])
def api_delete_prompt(prompt_id):
    db.delete_prompt(prompt_id)
    return jsonify({"ok": True})


@app.route("/api/prompts/reorder", methods=["POST"])
def api_reorder_prompts():
    data = request.json
    if data is None:
        return jsonify({"error": "JSON body required."}), 400
    ordered_ids = data.get("ordered_ids", [])
    if not ordered_ids:
        return jsonify({"error": "ordered_ids required"}), 400
    db.reorder_prompts([int(i) for i in ordered_ids])
    return jsonify({"ok": True})


@app.route("/api/prompts/export", methods=["GET"])
def api_export_prompts():
    account_id = request.args.get("account_id")
    account_name = request.args.get("name", "all")

    if account_id:
        prompts = db.list_prompts(account_id=int(account_id))
    else:
        prompts = db.list_prompts()

    export_fields = ["name", "instructions", "label_name", "active", "action_archive",
                     "action_spam", "action_trash", "action_mark_read", "stop_processing", "account_id"]
    export_data = [{k: p[k] for k in export_fields if k in p} for p in prompts]

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
        "poll_interval": int(db.get_setting("poll_interval", str(POLL_INTERVAL))),
        "ollama_model": OLLAMA_MODEL,
        "ollama_host": OLLAMA_HOST,
        "ollama_timeout": OLLAMA_TIMEOUT,
        "ollama_num_ctx": OLLAMA_NUM_CTX,
        "ollama_num_predict": OLLAMA_NUM_PREDICT,
        "gmail_max_results": GMAIL_MAX_RESULTS,
        "gmail_lookback_hours": GMAIL_LOOKBACK_HOURS,
    })


@app.route("/api/settings", methods=["POST"])
def api_update_settings():
    data = request.json
    if data is None:
        return jsonify({"error": "JSON body required."}), 400
    if "poll_interval" in data:
        val = int(data["poll_interval"])
        if val < MIN_POLL_INTERVAL:
            return jsonify({"error": f"Minimum poll interval is {MIN_POLL_INTERVAL} seconds"}), 400
        db.set_setting("poll_interval", str(val))
        db.add_log("INFO", f"Settings updated: poll_interval={val}s")
    return jsonify({"ok": True})


# ---- Logs + Status ----

@app.route("/api/logs", methods=["GET"])
def api_get_logs():
    limit = int(request.args.get("limit", 100))
    return jsonify(db.get_logs(limit))


@app.route("/api/logs/download", methods=["GET"])
def api_download_logs():
    import csv, io
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    logs = db.get_logs_range(start, end)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["timestamp", "level", "message"])
    for l in logs:
        w.writerow([l["timestamp"], l["level"], l["message"]])
    filename = f"logs_{start[:10]}_{end[:10]}.csv"
    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/status", methods=["GET"])
def api_status():
    import time
    return jsonify({**poller.get_status(), "current_time": time.time()})


@app.route("/api/scan", methods=["POST"])
def api_scan_now():
    db.add_log("INFO", "Manual scan triggered.")
    poller.run_now()
    return jsonify({"ok": True})


# ---- Prompt Builder ----

@app.route("/api/prompts/generate", methods=["POST"])
def api_generate_prompt():
    data = request.json
    if data is None:
        return jsonify({"error": "JSON body required."}), 400
    description = data.get("description", "").strip()
    if not description:
        return jsonify({"error": "description is required"}), 400

    def generate():
        try:
            for event in llm_client.stream_generate_prompt_instruction(description):
                yield f"data: {json.dumps(event)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            db.add_log("ERROR", f"Prompt generation failed: {e}")
            yield f"data: {json.dumps({'type': 'error', 'text': 'Generation failed. Check Ollama is running.'})}\n\n"

    return Response(generate(), content_type="text/event-stream")


# ---- Account Gmail labels ----

@app.route("/api/accounts/<int:account_id>/labels", methods=["GET"])
def api_list_account_labels(account_id):
    account = db.get_account(account_id)
    if not account:
        return jsonify({"error": "Not found"}), 404
    try:
        service, refreshed_creds = gmail_client.get_service(account["credentials_json"])
        if json.loads(refreshed_creds) != json.loads(account["credentials_json"]):
            db.update_account_credentials(account_id, refreshed_creds)
        return jsonify(gmail_client.list_labels(service))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---- Retention ----

@app.route("/api/retention/<int:account_id>", methods=["GET"])
def api_get_retention(account_id):
    if not db.get_account(account_id):
        return jsonify({"error": "Not found"}), 404
    return jsonify(db.get_retention(account_id))


@app.route("/api/retention/<int:account_id>", methods=["POST"])
def api_set_retention(account_id):
    if not db.get_account(account_id):
        return jsonify({"error": "Not found"}), 404
    data = request.json
    if data is None:
        return jsonify({"error": "JSON body required."}), 400
    global_days = data.get("global_days")
    if global_days is None:
        db.clear_global_retention(account_id)
    else:
        days = int(global_days)
        if days < 1:
            return jsonify({"error": "global_days must be at least 1"}), 400
        db.set_global_retention(account_id, days)
    return jsonify({"ok": True})


@app.route("/api/retention/<int:account_id>/labels", methods=["POST"])
def api_add_label_retention(account_id):
    if not db.get_account(account_id):
        return jsonify({"error": "Not found"}), 404
    data = request.json
    if data is None:
        return jsonify({"error": "JSON body required."}), 400
    label_name = (data.get("label_name") or "").strip()
    days = data.get("days")
    if not label_name or days is None:
        return jsonify({"error": "label_name and days are required"}), 400
    days = int(days)
    if days < 1:
        return jsonify({"error": "days must be at least 1"}), 400
    db.add_label_retention(account_id, label_name, days)
    return jsonify({"ok": True}), 201


@app.route("/api/retention/<int:account_id>/labels/<int:rule_id>", methods=["DELETE"])
def api_delete_label_retention(account_id, rule_id):
    db.delete_label_retention(rule_id)
    return jsonify({"ok": True})


@app.route("/api/retention/<int:account_id>/exemptions", methods=["POST"])
def api_add_exemption(account_id):
    if not db.get_account(account_id):
        return jsonify({"error": "Not found"}), 404
    data = request.json
    if data is None:
        return jsonify({"error": "JSON body required."}), 400
    label_name = (data.get("label_name") or "").strip()
    if not label_name:
        return jsonify({"error": "label_name is required"}), 400
    db.add_label_exemption(account_id, label_name)
    return jsonify({"ok": True}), 201


@app.route("/api/retention/<int:account_id>/exemptions/<int:exemption_id>", methods=["DELETE"])
def api_delete_exemption(account_id, exemption_id):
    db.delete_label_exemption(exemption_id)
    return jsonify({"ok": True})


# ---- History ----

@app.route("/api/history", methods=["GET"])
def api_get_history():
    account_id = request.args.get("account_id")
    prompt_id = request.args.get("prompt_id")
    subject = request.args.get("subject", "").strip()
    sender = request.args.get("sender", "").strip()
    limit = min(int(request.args.get("limit", 200)), HISTORY_MAX_LIMIT)
    rows = db.get_categorization_history(
        account_id=int(account_id) if account_id else None,
        prompt_id=int(prompt_id) if prompt_id else None,
        subject=subject or None,
        sender=sender or None,
        limit=limit,
    )
    return jsonify(rows)


# ---- Startup ----

def _get_or_create_secret_key() -> str:
    """Generate a stable secret key on first run and persist it in the DB."""
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
