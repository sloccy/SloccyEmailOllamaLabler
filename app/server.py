import os
import secrets
from urllib.parse import urlparse, parse_qs
from flask import Flask, jsonify, request, render_template, session
from app import db, gmail_client, poller, llm_client

app = Flask(__name__, template_folder="templates")
app.secret_key = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(32))


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# OAuth - Desktop app flow (no redirect URI registration needed)
# ---------------------------------------------------------------------------

@app.route("/api/oauth/start", methods=["POST"])
def oauth_start():
    """Generate and return the Google auth URL for the user to open manually."""
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    try:
        auth_url = gmail_client.get_auth_url(state)
        return jsonify({"auth_url": auth_url, "state": state})
    except FileNotFoundError:
        return jsonify({"error": "credentials.json not found. Place your Google OAuth credentials file at /credentials/credentials.json inside the container (./credentials/ on the host)."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/oauth/exchange", methods=["POST"])
def oauth_exchange():
    """
    Accept the full redirect URL that the user copied from their browser
    after approving access. Extract the code and exchange it for tokens.
    """
    data = request.json
    pasted_url = data.get("url", "").strip()

    if not pasted_url:
        return jsonify({"error": "No URL provided."}), 400

    # Parse the code and state out of the pasted URL
    try:
        parsed = urlparse(pasted_url)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
    except Exception:
        return jsonify({"error": "Could not parse the URL. Make sure you copied the full URL from your browser's address bar."}), 400

    if not code:
        return jsonify({"error": "No authorization code found in the URL. Make sure you copied the full URL from your browser's address bar after approving access."}), 400

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


# ---------------------------------------------------------------------------
# Accounts API
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Prompts API
# ---------------------------------------------------------------------------

@app.route("/api/prompts", methods=["GET"])
def api_list_prompts():
    return jsonify(db.list_prompts())


@app.route("/api/prompts", methods=["POST"])
def api_create_prompt():
    data = request.json
    if not data.get("name") or not data.get("instructions") or not data.get("label_name"):
        return jsonify({"error": "name, instructions, and label_name are required"}), 400
    db.create_prompt(data["name"], data["instructions"], data["label_name"])
    db.add_log("INFO", f"Prompt created: {data['name']} → label '{data['label_name']}'")
    return jsonify({"ok": True}), 201


@app.route("/api/prompts/<int:prompt_id>", methods=["PUT"])
def api_update_prompt(prompt_id):
    data = request.json
    db.update_prompt(
        prompt_id,
        data["name"],
        data["instructions"],
        data["label_name"],
        int(data.get("active", 1)),
    )
    return jsonify({"ok": True})


@app.route("/api/prompts/<int:prompt_id>", methods=["DELETE"])
def api_delete_prompt(prompt_id):
    db.delete_prompt(prompt_id)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------

@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    return jsonify({
        "poll_interval": int(db.get_setting("poll_interval", "300")),
        "ollama_model": os.getenv("OLLAMA_MODEL", "llama3.2"),
        "ollama_host": os.getenv("OLLAMA_HOST", "http://localhost:11434"),
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


# ---------------------------------------------------------------------------
# Logs + Status
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def create_app():
    db.init_db()
    import threading
    threading.Thread(target=llm_client.ensure_model_pulled, daemon=True).start()
    poller.start()
    return app


if __name__ == "__main__":
    application = create_app()
    application.run(host="0.0.0.0", port=5000, debug=False)
