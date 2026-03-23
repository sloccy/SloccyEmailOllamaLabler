import csv
import gzip
import hashlib
import html as _html
import io
import json
import os
import re
import secrets
import threading
import time as _time
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs
from flask import Flask, jsonify, request, render_template, session, Response, make_response
from app import db, gmail_client, poller, llm_client
from app.config import (POLL_INTERVAL, OLLAMA_MODEL, OLLAMA_HOST, OLLAMA_TIMEOUT,
                        OLLAMA_NUM_CTX, OLLAMA_NUM_PREDICT, GMAIL_MAX_RESULTS,
                        GMAIL_LOOKBACK_HOURS, MIN_POLL_INTERVAL, HISTORY_MAX_LIMIT)

app = Flask(__name__, template_folder="templates")
app.secret_key = "placeholder-replaced-at-startup"

_ASSET_VERSION = str(int(_time.time()))
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_static_cache: dict[str, tuple[bytes, bytes | None, str, str]] = {}

_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
}


def _minify_css(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*([{}:;,>~+])\s*", r"\1", text)
    text = re.sub(r";}", "}", text)
    return text.strip()


def _load_static(filename: str) -> tuple[bytes, bytes | None, str, str]:
    filepath = os.path.join(_STATIC_DIR, filename)
    with open(filepath, "rb") as f:
        raw = f.read()
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".css" and not filename.endswith(".min.css"):
        raw = _minify_css(raw.decode("utf-8")).encode("utf-8")
    gz = gzip.compress(raw) if len(raw) >= 500 else None
    etag = hashlib.md5(raw).hexdigest()[:12]
    ct = _CONTENT_TYPES.get(ext, "application/octet-stream")
    return raw, gz, etag, ct


@app.context_processor
def _inject_asset_version():
    return {"asset_v": _ASSET_VERSION}


@app.route("/static/<path:filename>")
def serve_static(filename):
    if filename not in _static_cache:
        try:
            _static_cache[filename] = _load_static(filename)
        except (FileNotFoundError, IsADirectoryError):
            from flask import abort
            abort(404)
    raw, gz, etag, ct = _static_cache[filename]
    versioned = "v=" in request.query_string.decode()
    cc = "public, max-age=31536000, immutable" if versioned else "public, max-age=86400"
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag, "Cache-Control": cc})
    use_gz = gz is not None and "gzip" in request.headers.get("Accept-Encoding", "")
    headers = {"Content-Type": ct, "Cache-Control": cc, "ETag": etag, "Vary": "Accept-Encoding"}
    if use_gz:
        headers["Content-Encoding"] = "gzip"
        return Response(gz, headers=headers)
    return Response(raw, headers=headers)


# ---- Fragment helpers ----

def _fmt_interval(secs):
    secs = int(secs)
    if secs >= 3600:
        return f"{round(secs / 3600)}h"
    if secs >= 60:
        return f"{round(secs / 60)}m"
    return f"{secs}s"


def _fmt_date(ts):
    if not ts:
        return "—"
    try:
        s = ts.replace("Z", "") if ts.endswith("Z") else ts
        d = datetime.fromisoformat(s)
        return d.strftime("%-d %b, %H:%M")
    except Exception:
        return str(ts)


def _fmt_retention(days):
    days = int(days)
    if days >= 365 and days % 365 == 0:
        v = days // 365
        return f"{v} {'year' if v == 1 else 'years'}"
    return f"{days} {'day' if days == 1 else 'days'}"


app.jinja_env.filters["fmtdate"] = _fmt_date
app.jinja_env.filters["fmtinterval"] = _fmt_interval
app.jinja_env.filters["fmtretention"] = _fmt_retention


@app.after_request
def compress_response(response):
    if "gzip" not in request.headers.get("Accept-Encoding", ""):
        return response
    if response.status_code < 200 or response.status_code >= 300:
        return response
    if "Content-Encoding" in response.headers:
        return response
    if response.direct_passthrough:
        return response
    data = response.get_data()
    if len(data) < 500:
        return response
    compressed = gzip.compress(data)
    response.set_data(compressed)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = len(compressed)
    response.headers["Vary"] = "Accept-Encoding"
    return response


def fragment_response(template, ctx, toast=None):
    resp = make_response(render_template(template, **ctx))
    if toast:
        if isinstance(toast, str):
            toast = {"message": toast, "type": "success"}
        resp.headers["HX-Trigger"] = json.dumps({"showToast": toast})
    return resp


def _safe_accounts():
    return db.list_accounts_safe()


# ---- UI ----

@app.route("/")
def index():
    return render_template("index.html")


def _ensure_label_for_accounts(account_id, label_name):
    def _do():
        if account_id is not None:
            accounts = [db.get_account(account_id)]
        else:
            accounts = [a for a in db.list_accounts() if a.get("active")]
        for account in accounts:
            if not account:
                continue
            try:
                creds, refreshed_creds = gmail_client.get_service(account["credentials_json"])
                if refreshed_creds and refreshed_creds != account["credentials_json"]:
                    db.update_account_credentials(account["id"], refreshed_creds)
                gmail_client.build_label_cache(creds, [label_name])
            except Exception as e:
                db.add_log("WARNING", f"Could not pre-create label '{label_name}' for account {account.get('id')}: {e}")
    threading.Thread(target=_do, daemon=True).start()


# ---- API routes (fetch / download) ----

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


@app.route("/api/config/export", methods=["GET"])
def api_export_config():
    accounts_raw = db.list_accounts()
    account_map = {a["id"]: a["email"] for a in accounts_raw}

    accounts_export = [{"email": a["email"], "active": a["active"]} for a in accounts_raw]

    prompts_raw = db.list_prompts()
    prompts_export = []
    for p in prompts_raw:
        aid = p.get("account_id")
        prompts_export.append({
            "name": p["name"],
            "instructions": p["instructions"],
            "label_name": p["label_name"],
            "active": p["active"],
            "action_archive": p.get("action_archive", 0),
            "action_spam": p.get("action_spam", 0),
            "action_trash": p.get("action_trash", 0),
            "action_mark_read": p.get("action_mark_read", 0),
            "stop_processing": p.get("stop_processing", 0),
            "account": account_map.get(aid, "all accounts") if aid else "all accounts",
            "sort_order": p.get("sort_order", 0),
        })

    with db.get_db_readonly() as conn:
        settings_rows = conn.execute("SELECT key, value FROM settings").fetchall()
    settings_export = {r["key"]: r["value"] for r in settings_rows
                       if r["key"] != "flask_secret_key"}

    retention_export = []
    for a in accounts_raw:
        ret = db.get_retention(a["id"])
        retention_export.append({
            "account": a["email"],
            "global_days": ret["global_days"],
            "label_rules": [{"label_name": lr["label_name"], "days": lr["days"]}
                            for lr in ret["labels"]],
            "exemptions": [{"label_name": ex["label_name"]} for ex in ret["exemptions"]],
        })

    payload = {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "accounts": accounts_export,
        "prompts": prompts_export,
        "settings": settings_export,
        "retention": retention_export,
    }
    date_str = datetime.now().strftime("%Y-%m-%d")
    return Response(
        json.dumps(payload, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename=config-backup-{date_str}.json"},
    )


@app.route("/api/config/import", methods=["POST"])
def api_import_config():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400
    f = request.files["file"]
    try:
        data = json.loads(f.read())
    except Exception:
        return jsonify({"error": "Invalid JSON file."}), 400
    if "version" not in data:
        return jsonify({"error": "Missing version key — not a valid config backup."}), 400

    summary = {"accounts": {"added": 0, "skipped": 0},
               "prompts": {"added": 0, "skipped": 0},
               "settings": {"added": 0, "skipped": 0},
               "retention": {"added": 0, "skipped": 0}}

    # Accounts — build email→id mapping
    email_to_id = {a["email"]: a["id"] for a in db.list_accounts()}
    for acct in data.get("accounts", []):
        email = acct.get("email", "").strip()
        if not email:
            continue
        if email in email_to_id:
            summary["accounts"]["skipped"] += 1
        else:
            new_id = db.create_account_placeholder(email)
            email_to_id[email] = new_id
            summary["accounts"]["added"] += 1

    # Prompts
    for p in data.get("prompts", []):
        acct_str = p.get("account", "all accounts")
        account_id = email_to_id.get(acct_str) if acct_str != "all accounts" else None
        if db.prompt_exists(p["name"], account_id):
            summary["prompts"]["skipped"] += 1
        else:
            db.create_prompt(
                p["name"], p["instructions"], p["label_name"],
                action_archive=p.get("action_archive", 0),
                action_spam=p.get("action_spam", 0),
                action_trash=p.get("action_trash", 0),
                action_mark_read=p.get("action_mark_read", 0),
                stop_processing=p.get("stop_processing", 0),
                account_id=account_id,
            )
            summary["prompts"]["added"] += 1

    # Settings
    for key, value in data.get("settings", {}).items():
        if key == "flask_secret_key":
            continue
        if db.get_setting(key) is None:
            db.set_setting(key, value)
            summary["settings"]["added"] += 1
        else:
            summary["settings"]["skipped"] += 1

    # Retention
    for ret in data.get("retention", []):
        email = ret.get("account", "")
        account_id = email_to_id.get(email)
        if not account_id:
            continue
        if ret.get("global_days") is not None and not db.has_global_retention(account_id):
            db.set_global_retention(account_id, ret["global_days"])
            summary["retention"]["added"] += 1
        elif ret.get("global_days") is not None:
            summary["retention"]["skipped"] += 1
        for lr in ret.get("label_rules", []):
            if not db.label_retention_exists(account_id, lr["label_name"]):
                db.add_label_retention(account_id, lr["label_name"], lr["days"])
                summary["retention"]["added"] += 1
            else:
                summary["retention"]["skipped"] += 1
        for ex in ret.get("exemptions", []):
            db.add_label_exemption(account_id, ex["label_name"])

    db.add_log("INFO", f"Config imported: {summary}")
    return jsonify({"ok": True, "summary": summary})


@app.route("/api/logs/download", methods=["GET"])
def api_download_logs():
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


# ---- Fragment routes ----

@app.route("/fragments/dashboard")
def frag_dashboard():
    accounts = _safe_accounts()
    prompts = db.list_prompts()
    poll_interval = int(db.get_setting("poll_interval", str(POLL_INTERVAL)))
    status = {**poller.get_status(), "current_time": _time.time()}
    logs = db.get_logs(15)
    if status.get("next_run"):
        secs = max(0, round(status["next_run"] - status["current_time"]))
        next_scan = _fmt_interval(secs) if secs > 0 else "now"
    else:
        next_scan = "—"
    return fragment_response("fragments/dashboard.html", {
        "accounts": accounts,
        "active_prompts": sum(1 for p in prompts if p["active"]),
        "poll_interval": _fmt_interval(poll_interval),
        "next_scan": next_scan,
        "poller_running": status.get("running", False),
        "logs": logs,
    })


@app.route("/fragments/accounts")
def frag_accounts():
    accounts = _safe_accounts()
    return fragment_response("fragments/accounts_list.html", {"accounts": accounts})


@app.route("/fragments/accounts/<int:account_id>/toggle", methods=["POST"])
def frag_toggle_account(account_id):
    new_state = db.toggle_account(account_id)
    accounts = _safe_accounts()
    msg = "Account resumed." if new_state else "Account paused."
    return fragment_response("fragments/accounts_list.html", {"accounts": accounts}, toast=msg)


@app.route("/fragments/accounts/<int:account_id>", methods=["DELETE"])
def frag_delete_account(account_id):
    db.delete_account(account_id)
    db.add_log("INFO", f"Account {account_id} removed.")
    accounts = _safe_accounts()
    return fragment_response("fragments/accounts_list.html", {"accounts": accounts},
                             toast="Account removed.")


@app.route("/fragments/prompts")
def frag_prompts():
    account_id = request.args.get("account_id", "")
    prompts = db.list_prompts(account_id=int(account_id)) if account_id else db.list_prompts()
    accounts = _safe_accounts()
    return fragment_response("fragments/prompts_list.html",
                             {"prompts": prompts, "accounts": accounts})


@app.route("/fragments/prompts", methods=["POST"])
def frag_create_prompt():
    f = request.form
    name = f.get("name", "").strip()
    instructions = f.get("instructions", "").strip()
    label_name = f.get("label_name", "").strip()
    if not name or not instructions or not label_name:
        return fragment_response("fragments/prompts_list.html",
                                 {"prompts": db.list_prompts(),
                                  "accounts": _safe_accounts()},
                                 toast={"message": "name, instructions, and label_name are required",
                                        "type": "error"})
    account_id = f.get("account_id") or None
    db.create_prompt(
        name, instructions, label_name,
        action_archive=int(bool(f.get("action_archive"))),
        action_spam=int(bool(f.get("action_spam"))),
        action_trash=int(bool(f.get("action_trash"))),
        action_mark_read=int(bool(f.get("action_mark_read"))),
        stop_processing=int(bool(f.get("stop_processing"))),
        account_id=int(account_id) if account_id else None,
    )
    _ensure_label_for_accounts(int(account_id) if account_id else None, label_name)
    scope = f"account {account_id}" if account_id else "all accounts"
    db.add_log("INFO", f"Prompt created: {name} → label '{label_name}' ({scope})")
    prompts = db.list_prompts()
    accounts = _safe_accounts()
    return fragment_response("fragments/prompts_list.html",
                             {"prompts": prompts, "accounts": accounts},
                             toast="Rule created.")


@app.route("/fragments/prompts/<int:prompt_id>", methods=["PUT"])
def frag_update_prompt(prompt_id):
    f = request.form
    name = f.get("name", "").strip()
    instructions = f.get("instructions", "").strip()
    label_name = f.get("label_name", "").strip()
    active = int(f.get("active", 1))
    account_id = f.get("account_id") or None
    db.update_prompt(
        prompt_id, name, instructions, label_name, active,
        action_archive=int(bool(f.get("action_archive"))),
        action_spam=int(bool(f.get("action_spam"))),
        action_trash=int(bool(f.get("action_trash"))),
        action_mark_read=int(bool(f.get("action_mark_read"))),
        stop_processing=int(bool(f.get("stop_processing"))),
        account_id=int(account_id) if account_id else None,
    )
    _ensure_label_for_accounts(int(account_id) if account_id else None, label_name)
    prompts = db.list_prompts()
    accounts = _safe_accounts()
    return fragment_response("fragments/prompts_list.html",
                             {"prompts": prompts, "accounts": accounts},
                             toast="Rule updated.")


@app.route("/fragments/prompts/<int:prompt_id>", methods=["DELETE"])
def frag_delete_prompt(prompt_id):
    db.delete_prompt(prompt_id)
    prompts = db.list_prompts()
    accounts = _safe_accounts()
    return fragment_response("fragments/prompts_list.html",
                             {"prompts": prompts, "accounts": accounts},
                             toast="Rule deleted.")


@app.route("/fragments/prompts/<int:prompt_id>/toggle", methods=["POST"])
def frag_toggle_prompt(prompt_id):
    new_active = db.toggle_prompt(prompt_id)
    if new_active is None:
        return "", 404
    p = db.get_prompt(prompt_id)
    accounts = _safe_accounts()
    account_map = {a["id"]: a["email"] for a in accounts}
    msg = "Rule paused." if not new_active else "Rule resumed."
    return fragment_response("fragments/prompt_card_view.html",
                             {"p": p, "accounts": accounts, "account_map": account_map},
                             toast=msg)


@app.route("/fragments/prompts/<int:prompt_id>/edit")
def frag_prompt_edit(prompt_id):
    p = db.get_prompt(prompt_id)
    if not p:
        return "", 404
    accounts = _safe_accounts()
    account_map = {a["id"]: a["email"] for a in accounts}
    return fragment_response("fragments/prompt_card_edit.html",
                             {"p": p, "accounts": accounts, "account_map": account_map})


@app.route("/fragments/prompts/<int:prompt_id>/view")
def frag_prompt_view(prompt_id):
    p = db.get_prompt(prompt_id)
    if not p:
        return "", 404
    accounts = _safe_accounts()
    account_map = {a["id"]: a["email"] for a in accounts}
    return fragment_response("fragments/prompt_card_view.html",
                             {"p": p, "accounts": accounts, "account_map": account_map})


@app.route("/fragments/settings")
def frag_get_settings():
    return fragment_response("fragments/settings_form.html", {
        "poll_interval": int(db.get_setting("poll_interval", str(POLL_INTERVAL))),
        "ollama_model": OLLAMA_MODEL,
        "ollama_host": OLLAMA_HOST,
    })


@app.route("/fragments/settings", methods=["PATCH"])
def frag_update_settings():
    f = request.form
    if "poll_interval" in f:
        val = int(f["poll_interval"])
        if val < MIN_POLL_INTERVAL:
            return fragment_response("fragments/settings_form.html", {
                "poll_interval": val,
                "ollama_model": OLLAMA_MODEL,
                "ollama_host": OLLAMA_HOST,
            }, toast={"message": f"Minimum poll interval is {MIN_POLL_INTERVAL} seconds",
                      "type": "error"})
        db.set_setting("poll_interval", str(val))
        db.add_log("INFO", f"Settings updated: poll_interval={val}s")
    return fragment_response("fragments/settings_form.html", {
        "poll_interval": int(db.get_setting("poll_interval", str(POLL_INTERVAL))),
        "ollama_model": OLLAMA_MODEL,
        "ollama_host": OLLAMA_HOST,
    }, toast="Settings saved.")


@app.route("/fragments/logs")
def frag_logs():
    logs = db.get_logs(100)
    return fragment_response("fragments/logs_list.html", {"logs": logs})


@app.route("/fragments/history")
def frag_history():
    account_id = request.args.get("account_id", "")
    prompt_id = request.args.get("prompt_id", "")
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
    for r in rows:
        r["extra_actions"] = [
            s for a in (r.get("actions") or "").split(",")
            if (s := a.strip()) and not s.startswith("labeled →")
        ]
    return fragment_response("fragments/history_table.html", {"rows": rows})


@app.route("/fragments/history/filters")
def frag_history_filters():
    accounts = _safe_accounts()
    prompts = db.list_prompts()
    return fragment_response("fragments/history_filters.html",
                             {"accounts": accounts, "prompts": prompts})


@app.route("/fragments/retention/<int:account_id>")
def frag_retention(account_id):
    account = db.get_account(account_id)
    if not account:
        return "", 404
    retention = db.get_retention(account_id)
    try:
        creds, refreshed_creds = gmail_client.get_service(account["credentials_json"])
        if refreshed_creds and refreshed_creds != account["credentials_json"]:
            db.update_account_credentials(account_id, refreshed_creds)
        gmail_labels = gmail_client.list_labels(creds)
    except Exception:
        gmail_labels = []
    return fragment_response("fragments/retention_panel.html",
                             {"retention": retention, "account_id": account_id,
                              "gmail_labels": gmail_labels})


@app.route("/fragments/retention/<int:account_id>", methods=["POST"])
def frag_set_retention(account_id):
    account = db.get_account(account_id)
    if not account:
        return "", 404
    f = request.form
    enabled = bool(f.get("enabled"))
    if enabled:
        value = int(f.get("value", 1))
        unit = f.get("unit", "days")
        days = value * 365 if unit == "years" else value
        if days < 1:
            return fragment_response("fragments/retention_panel.html",
                                     {"retention": db.get_retention(account_id),
                                      "account_id": account_id, "gmail_labels": []},
                                     toast={"message": "Days must be at least 1.", "type": "error"})
        db.set_global_retention(account_id, days)
    else:
        db.clear_global_retention(account_id)
    retention = db.get_retention(account_id)
    try:
        creds, _ = gmail_client.get_service(account["credentials_json"])
        gmail_labels = gmail_client.list_labels(creds)
    except Exception:
        gmail_labels = []
    msg = "Global retention saved." if enabled else "Global retention disabled."
    return fragment_response("fragments/retention_panel.html",
                             {"retention": retention, "account_id": account_id,
                              "gmail_labels": gmail_labels},
                             toast=msg)


@app.route("/fragments/retention/<int:account_id>/labels", methods=["POST"])
def frag_add_label_retention(account_id):
    account = db.get_account(account_id)
    if not account:
        return "", 404
    f = request.form
    label_name = (f.get("label_name") or "").strip()
    value = f.get("value", "")
    unit = f.get("unit", "days")
    if not label_name or not value:
        return fragment_response("fragments/retention_panel.html",
                                 {"retention": db.get_retention(account_id),
                                  "account_id": account_id, "gmail_labels": []},
                                 toast={"message": "Label and days are required.", "type": "error"})
    days = int(value) * 365 if unit == "years" else int(value)
    if days >= 1:
        db.add_label_retention(account_id, label_name, days)
    retention = db.get_retention(account_id)
    try:
        creds, _ = gmail_client.get_service(account["credentials_json"])
        gmail_labels = gmail_client.list_labels(creds)
    except Exception:
        gmail_labels = []
    return fragment_response("fragments/retention_panel.html",
                             {"retention": retention, "account_id": account_id,
                              "gmail_labels": gmail_labels},
                             toast="Label rule added.")


@app.route("/fragments/retention/<int:account_id>/labels/<int:rule_id>", methods=["DELETE"])
def frag_delete_label_retention(account_id, rule_id):
    db.delete_label_retention(rule_id)
    retention = db.get_retention(account_id)
    account = db.get_account(account_id)
    try:
        creds, _ = gmail_client.get_service(account["credentials_json"])
        gmail_labels = gmail_client.list_labels(creds)
    except Exception:
        gmail_labels = []
    return fragment_response("fragments/retention_panel.html",
                             {"retention": retention, "account_id": account_id,
                              "gmail_labels": gmail_labels},
                             toast="Rule removed.")


@app.route("/fragments/retention/<int:account_id>/exemptions", methods=["POST"])
def frag_add_exemption(account_id):
    account = db.get_account(account_id)
    if not account:
        return "", 404
    label_name = (request.form.get("label_name") or "").strip()
    if label_name:
        db.add_label_exemption(account_id, label_name)
    retention = db.get_retention(account_id)
    try:
        creds, _ = gmail_client.get_service(account["credentials_json"])
        gmail_labels = gmail_client.list_labels(creds)
    except Exception:
        gmail_labels = []
    return fragment_response("fragments/retention_panel.html",
                             {"retention": retention, "account_id": account_id,
                              "gmail_labels": gmail_labels},
                             toast=f'"{label_name}" will never be deleted.')


@app.route("/fragments/retention/<int:account_id>/exemptions/<int:exemption_id>", methods=["DELETE"])
def frag_delete_exemption(account_id, exemption_id):
    db.delete_label_exemption(exemption_id)
    retention = db.get_retention(account_id)
    account = db.get_account(account_id)
    try:
        creds, _ = gmail_client.get_service(account["credentials_json"])
        gmail_labels = gmail_client.list_labels(creds)
    except Exception:
        gmail_labels = []
    return fragment_response("fragments/retention_panel.html",
                             {"retention": retention, "account_id": account_id,
                              "gmail_labels": gmail_labels},
                             toast="Exemption removed.")


@app.route("/fragments/oauth/start", methods=["POST"])
def frag_oauth_start():
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    try:
        auth_url = gmail_client.get_auth_url(state)
        return fragment_response("fragments/oauth_step2.html", {"auth_url": auth_url})
    except FileNotFoundError:
        resp = make_response("credentials.json not found.", 500)
        resp.headers["HX-Trigger"] = json.dumps({"showToast": {
            "message": "credentials.json not found. Place your Google OAuth credentials file at /credentials/credentials.json.",
            "type": "error"
        }})
        return resp
    except Exception as e:
        resp = make_response(str(e), 500)
        resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": str(e), "type": "error"}})
        return resp


@app.route("/fragments/oauth/exchange", methods=["POST"])
def frag_oauth_exchange():
    pasted_url = (request.form.get("url") or "").strip()
    if not pasted_url:
        resp = make_response("", 400)
        resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": "No URL provided.", "type": "error"}})
        return resp
    try:
        parsed = urlparse(pasted_url)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
    except Exception:
        resp = make_response("", 400)
        resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": "Could not parse the URL.", "type": "error"}})
        return resp
    if not code:
        resp = make_response("", 400)
        resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": "No authorization code found in the URL.", "type": "error"}})
        return resp
    expected_state = session.get("oauth_state")
    if not expected_state or state != expected_state:
        resp = make_response("", 400)
        resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": "State mismatch. Please start the authorization process again.", "type": "error"}})
        return resp
    try:
        email, credentials_json = gmail_client.exchange_code(state, code)
        db.upsert_account(email, credentials_json)
        db.add_log("INFO", f"Account connected: {email}")
        accounts = _safe_accounts()
        resp = make_response(render_template("fragments/accounts_list.html", accounts=accounts))
        resp.headers["HX-Trigger"] = json.dumps({
            "showToast": {"message": f"{email} connected.", "type": "success"},
            "closeOAuthPanel": True,
        })
        return resp
    except Exception as e:
        db.add_log("ERROR", f"OAuth exchange failed: {e}")
        resp = make_response("", 500)
        resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": str(e), "type": "error"}})
        return resp


@app.route("/fragments/scan", methods=["POST"])
def frag_scan():
    db.add_log("INFO", "Manual scan triggered.")
    poller.run_now()
    resp = make_response("", 204)
    resp.headers["HX-Trigger"] = json.dumps({"showToast": {
        "message": "Scan triggered. Check logs for results.",
        "type": "success"
    }})
    return resp


@app.route("/fragments/account-options")
def frag_account_options():
    opt_type = request.args.get("type", "filter")
    accounts = _safe_accounts()
    if opt_type == "new-prompt":
        first = '<option value="">All accounts (global)</option>'
    elif opt_type == "retention":
        first = '<option value="">Select an account\u2026</option>'
    else:
        first = '<option value="">All accounts</option>'
    options = first + "".join(
        f'<option value="{a["id"]}">{_html.escape(a["email"])}</option>'
        for a in accounts
    )
    return Response(options, content_type="text/html")


@app.route("/fragments/retention-query")
def frag_retention_query():
    account_id = request.args.get("account_id", "").strip()
    if not account_id:
        return Response("", content_type="text/html")
    account_id = int(account_id)
    account = db.get_account(account_id)
    if not account:
        return Response("", content_type="text/html")
    retention = db.get_retention(account_id)
    try:
        creds, refreshed_creds = gmail_client.get_service(account["credentials_json"])
        if refreshed_creds and refreshed_creds != account["credentials_json"]:
            db.update_account_credentials(account_id, refreshed_creds)
        gmail_labels = gmail_client.list_labels(creds)
    except Exception:
        gmail_labels = []
    return fragment_response("fragments/retention_panel.html",
                             {"retention": retention, "account_id": account_id,
                              "gmail_labels": gmail_labels})


@app.route("/api/prompts/generate-stream")
def api_generate_prompt_stream():
    description = request.args.get("description", "").strip()

    def generate():
        if not description:
            yield "event: done\ndata: \n\n"
            return
        try:
            for event in llm_client.stream_generate_prompt_instruction(description):
                event_type = event.get("type", "content")
                text = event.get("text", "")
                lines = ["event: " + event_type] + [f"data: {l}" for l in text.split("\n")] + ["", ""]
                yield "\n".join(lines)
            yield "event: done\ndata: \n\n"
        except Exception as e:
            db.add_log("ERROR", f"Prompt generation failed: {e}")
            yield "event: error\ndata: Generation failed. Check Ollama is running.\n\n"
            yield "event: done\ndata: \n\n"

    return Response(generate(), content_type="text/event-stream")


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
