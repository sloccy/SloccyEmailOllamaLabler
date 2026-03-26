import csv
import io
import json
import secrets
import threading
import time as _time
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

from flask import Flask, Response, jsonify, make_response, render_template, request, session
from flask_compress import Compress

from app import db, gmail_client, poller
from app.config import HISTORY_MAX_LIMIT, MIN_POLL_INTERVAL, OLLAMA_HOST, OLLAMA_MODEL, POLL_INTERVAL
from app.llm import get_provider as _get_llm_provider

_llm = _get_llm_provider()

app = Flask(__name__, template_folder="templates")
app.secret_key = "placeholder-replaced-at-startup"
Compress(app)

_ASSET_VERSION = str(int(_time.time()))


@app.context_processor
def _inject_asset_version():
    return {"asset_v": _ASSET_VERSION}


@app.before_request
def _check_origin():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("Origin", "")
    if not origin:
        referer = request.headers.get("Referer", "")
        parsed = urlparse(referer)
        origin = f"{parsed.scheme}://{parsed.netloc}" if referer else ""
    if origin and urlparse(origin).netloc != request.host:
        return _htmx_toast("Cross-origin request blocked.", status=403)


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
        return f"{d.day} {d.strftime('%b')}, {d.strftime('%H:%M')}"
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


def fragment_response(template, ctx, toast=None):
    resp = make_response(render_template(template, **ctx))
    if toast:
        if isinstance(toast, str):
            toast = {"message": toast, "type": "success"}
        resp.headers["HX-Trigger"] = json.dumps({"showToast": toast})
    return resp


def _safe_accounts():
    return db.list_accounts_safe()


def _account_map(accounts=None):
    if accounts is None:
        accounts = db.list_accounts_safe()
    return {a["id"]: a["email"] for a in accounts}


def _htmx_toast(msg, category="error", status=400):
    resp = make_response("", status)
    resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": msg, "type": category}})
    return resp


def _settings_context():
    return {
        "poll_interval": int(db.get_setting("poll_interval", str(POLL_INTERVAL))),
        "ollama_model": OLLAMA_MODEL,
        "ollama_host": OLLAMA_HOST,
    }


def _parse_prompt_actions(form):
    account_id = form.get("account_id") or None
    return {
        "action_archive": int(bool(form.get("action_archive"))),
        "action_spam": int(bool(form.get("action_spam"))),
        "action_trash": int(bool(form.get("action_trash"))),
        "action_mark_read": int(bool(form.get("action_mark_read"))),
        "stop_processing": int(bool(form.get("stop_processing"))),
        "account_id": int(account_id) if account_id else None,
    }


# ---- UI ----


@app.route("/")
def index():
    return render_template("index.html")


def _ensure_label_for_accounts(account_id, label_name):
    thread_name = f"ensure_label_{account_id}_{label_name}"
    if any(t.name == thread_name and t.is_alive() for t in threading.enumerate()):
        return

    def _do():
        if account_id is not None:
            accounts = [db.get_account(account_id)]
        else:
            accounts = [a for a in db.list_accounts() if a.get("active")]
        for account in accounts:
            if not account:
                continue
            try:
                service = gmail_client.get_service_and_refresh(account)
                gmail_client.build_label_cache(service, [label_name])
            except Exception as e:
                db.add_log("WARNING", f"Could not pre-create label '{label_name}' for account {account.get('id')}: {e}")

    threading.Thread(target=_do, daemon=True, name=thread_name).start()


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

    export_fields = [
        "name",
        "instructions",
        "label_name",
        "active",
        "action_archive",
        "action_spam",
        "action_trash",
        "action_mark_read",
        "stop_processing",
        "account_id",
    ]
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
    account_map = _account_map(accounts_raw)

    accounts_export = [{"email": a["email"], "active": a["active"]} for a in accounts_raw]

    prompts_raw = db.list_prompts()
    prompts_export = []
    for p in prompts_raw:
        aid = p.get("account_id")
        prompts_export.append(
            {
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
            }
        )

    settings_export = {r["key"]: r["value"] for r in db.get_all_settings() if r["key"] != "flask_secret_key"}

    retention_export = []
    for a in accounts_raw:
        ret = db.get_retention(a["id"])
        retention_export.append(
            {
                "account": a["email"],
                "global_days": ret["global_days"],
                "label_rules": [{"label_name": lr["label_name"], "days": lr["days"]} for lr in ret["labels"]],
                "exemptions": [{"label_name": ex["label_name"]} for ex in ret["exemptions"]],
            }
        )

    now = datetime.now(UTC)
    payload = {
        "version": 1,
        "exported_at": now.isoformat(),
        "accounts": accounts_export,
        "prompts": prompts_export,
        "settings": settings_export,
        "retention": retention_export,
    }
    date_str = now.strftime("%Y-%m-%d")
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

    summary = {
        "accounts": {"added": 0, "skipped": 0},
        "prompts": {"added": 0, "skipped": 0},
        "settings": {"added": 0, "skipped": 0},
        "retention": {"added": 0, "skipped": 0},
    }

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
                p["name"],
                p["instructions"],
                p["label_name"],
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
    for row in logs:
        w.writerow([row["timestamp"], row["level"], row["message"]])
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
    poll_interval = int(db.get_setting("poll_interval", str(POLL_INTERVAL)))
    status = {**poller.get_status(), "current_time": _time.time()}
    logs = db.get_logs(15)
    if status.get("next_run"):
        secs = max(0, round(status["next_run"] - status["current_time"]))
        next_scan = _fmt_interval(secs) if secs > 0 else "now"
    else:
        next_scan = "—"
    return fragment_response(
        "fragments/dashboard.html",
        {
            "accounts": accounts,
            "active_prompts": db.count_active_prompts(),
            "poll_interval": _fmt_interval(poll_interval),
            "next_scan": next_scan,
            "poller_running": status.get("running", False),
            "logs": logs,
        },
    )


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
    return fragment_response("fragments/accounts_list.html", {"accounts": accounts}, toast="Account removed.")


@app.route("/fragments/prompts")
def frag_prompts():
    account_id = request.args.get("account_id", "")
    prompts = db.list_prompts(account_id=int(account_id)) if account_id else db.list_prompts()
    accounts = _safe_accounts()
    return fragment_response(
        "fragments/prompts_list.html", {"prompts": prompts, "accounts": accounts, "account_map": _account_map(accounts)}
    )


@app.route("/fragments/prompts", methods=["POST"])
def frag_create_prompt():
    f = request.form
    name = f.get("name", "").strip()
    instructions = f.get("instructions", "").strip()
    label_name = f.get("label_name", "").strip()
    if not name or not instructions or not label_name:
        accounts = _safe_accounts()
        return fragment_response(
            "fragments/prompts_list.html",
            {"prompts": db.list_prompts(), "accounts": accounts, "account_map": _account_map(accounts)},
            toast={"message": "name, instructions, and label_name are required", "type": "error"},
        )
    actions = _parse_prompt_actions(f)
    db.create_prompt(name, instructions, label_name, **actions)
    _ensure_label_for_accounts(actions["account_id"], label_name)
    scope = f"account {actions['account_id']}" if actions["account_id"] else "all accounts"
    db.add_log("INFO", f"Prompt created: {name} → label '{label_name}' ({scope})")
    prompts = db.list_prompts()
    accounts = _safe_accounts()
    return fragment_response(
        "fragments/prompts_list.html",
        {"prompts": prompts, "accounts": accounts, "account_map": _account_map(accounts)},
        toast="Rule created.",
    )


@app.route("/fragments/prompts/<int:prompt_id>", methods=["PUT"])
def frag_update_prompt(prompt_id):
    f = request.form
    name = f.get("name", "").strip()
    instructions = f.get("instructions", "").strip()
    label_name = f.get("label_name", "").strip()
    active = int(f.get("active", 1))
    actions = _parse_prompt_actions(f)
    db.update_prompt(prompt_id, name, instructions, label_name, active, **actions)
    _ensure_label_for_accounts(actions["account_id"], label_name)
    prompts = db.list_prompts()
    accounts = _safe_accounts()
    return fragment_response(
        "fragments/prompts_list.html",
        {"prompts": prompts, "accounts": accounts, "account_map": _account_map(accounts)},
        toast="Rule updated.",
    )


@app.route("/fragments/prompts/<int:prompt_id>", methods=["DELETE"])
def frag_delete_prompt(prompt_id):
    db.delete_prompt(prompt_id)
    prompts = db.list_prompts()
    accounts = _safe_accounts()
    return fragment_response(
        "fragments/prompts_list.html",
        {"prompts": prompts, "accounts": accounts, "account_map": _account_map(accounts)},
        toast="Rule deleted.",
    )


@app.route("/fragments/prompts/<int:prompt_id>/toggle", methods=["POST"])
def frag_toggle_prompt(prompt_id):
    new_active = db.toggle_prompt(prompt_id)
    if new_active is None:
        return _htmx_toast("Not found.", status=404)
    p = db.get_prompt(prompt_id)
    accounts = _safe_accounts()
    msg = "Rule paused." if not new_active else "Rule resumed."
    return fragment_response(
        "fragments/prompt_card_view.html",
        {"p": p, "accounts": accounts, "account_map": _account_map(accounts)},
        toast=msg,
    )


@app.route("/fragments/prompts/<int:prompt_id>/edit")
def frag_prompt_edit(prompt_id):
    p = db.get_prompt(prompt_id)
    if not p:
        return _htmx_toast("Not found.", status=404)
    accounts = _safe_accounts()
    return fragment_response(
        "fragments/prompt_card_edit.html", {"p": p, "accounts": accounts, "account_map": _account_map(accounts)}
    )


@app.route("/fragments/prompts/<int:prompt_id>/view")
def frag_prompt_view(prompt_id):
    p = db.get_prompt(prompt_id)
    if not p:
        return _htmx_toast("Not found.", status=404)
    accounts = _safe_accounts()
    return fragment_response(
        "fragments/prompt_card_view.html", {"p": p, "accounts": accounts, "account_map": _account_map(accounts)}
    )


@app.route("/fragments/settings")
def frag_get_settings():
    return fragment_response("fragments/settings_form.html", _settings_context())


@app.route("/fragments/settings", methods=["PATCH"])
def frag_update_settings():
    f = request.form
    if "poll_interval" in f:
        val = int(f["poll_interval"])
        if val < MIN_POLL_INTERVAL:
            ctx = _settings_context()
            ctx["poll_interval"] = val
            return fragment_response(
                "fragments/settings_form.html",
                ctx,
                toast={"message": f"Minimum poll interval is {MIN_POLL_INTERVAL} seconds", "type": "error"},
            )
        db.set_setting("poll_interval", str(val))
        db.add_log("INFO", f"Settings updated: poll_interval={val}s")
        poller.update_interval(val)
    return fragment_response("fragments/settings_form.html", _settings_context(), toast="Settings saved.")


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
            s for a in (r.get("actions") or "").split(",") if (s := a.strip()) and not s.startswith("labeled →")
        ]
    return fragment_response("fragments/history_table.html", {"rows": rows})


@app.route("/fragments/history/filters")
def frag_history_filters():
    accounts = _safe_accounts()
    prompts = db.list_prompts()
    return fragment_response("fragments/history_filters.html", {"accounts": accounts, "prompts": prompts})


def _retention_panel(account_id, account=None, service=None, toast=None):
    if account is None:
        account = db.get_account(account_id)
    if account is None:
        retention = db.get_retention(account_id)
        return fragment_response(
            "fragments/retention_panel.html",
            {"retention": retention, "account_id": account_id, "gmail_labels": []},
            toast=toast,
        )
    retention = db.get_retention(account_id)
    try:
        if service is None:
            service = gmail_client.get_service_and_refresh(account)
        gmail_labels = gmail_client.list_labels(service)
    except Exception:
        gmail_labels = []
    return fragment_response(
        "fragments/retention_panel.html",
        {"retention": retention, "account_id": account_id, "gmail_labels": gmail_labels},
        toast=toast,
    )


@app.route("/fragments/retention/<int:account_id>")
def frag_retention(account_id):
    account = db.get_account(account_id)
    if not account:
        return _htmx_toast("Not found.", status=404)
    service = None
    try:
        service = gmail_client.get_service_and_refresh(account)
    except Exception as e:
        db.add_log("WARNING", f"Could not refresh credentials for account {account_id}: {e}")
    return _retention_panel(account_id, account, service=service)


@app.route("/fragments/retention/<int:account_id>", methods=["POST"])
def frag_set_retention(account_id):
    account = db.get_account(account_id)
    if not account:
        return _htmx_toast("Not found.", status=404)
    f = request.form
    enabled = bool(f.get("enabled"))
    if enabled:
        try:
            value = int(f.get("value", 1))
        except ValueError:
            return _retention_panel(account_id, account, toast={"message": "Invalid value.", "type": "error"})
        days = value * 365 if f.get("unit") == "years" else value
        if days < 1:
            return _retention_panel(account_id, account, toast={"message": "Days must be at least 1.", "type": "error"})
        db.set_global_retention(account_id, days)
    else:
        db.clear_global_retention(account_id)
    msg = "Global retention saved." if enabled else "Global retention disabled."
    return _retention_panel(account_id, account, toast=msg)


@app.route("/fragments/retention/<int:account_id>/labels", methods=["POST"])
def frag_add_label_retention(account_id):
    account = db.get_account(account_id)
    if not account:
        return _htmx_toast("Not found.", status=404)
    f = request.form
    label_name = (f.get("label_name") or "").strip()
    value = f.get("value", "")
    if not label_name or not value:
        return _retention_panel(account_id, account, toast={"message": "Label and days are required.", "type": "error"})
    try:
        days = int(value) * 365 if f.get("unit") == "years" else int(value)
    except ValueError:
        return _retention_panel(account_id, account, toast={"message": "Invalid days value.", "type": "error"})
    if days >= 1:
        db.add_label_retention(account_id, label_name, days)
    return _retention_panel(account_id, account, toast="Label rule added.")


@app.route("/fragments/retention/<int:account_id>/labels/<int:rule_id>", methods=["DELETE"])
def frag_delete_label_retention(account_id, rule_id):
    db.delete_label_retention(rule_id)
    return _retention_panel(account_id, toast="Rule removed.")


@app.route("/fragments/retention/<int:account_id>/exemptions", methods=["POST"])
def frag_add_exemption(account_id):
    account = db.get_account(account_id)
    if not account:
        return _htmx_toast("Not found.", status=404)
    label_name = (request.form.get("label_name") or "").strip()
    if label_name:
        db.add_label_exemption(account_id, label_name)
    return _retention_panel(account_id, account, toast=f'"{label_name}" will never be deleted.')


@app.route("/fragments/retention/<int:account_id>/exemptions/<int:exemption_id>", methods=["DELETE"])
def frag_delete_exemption(account_id, exemption_id):
    db.delete_label_exemption(exemption_id)
    return _retention_panel(account_id, toast="Exemption removed.")


@app.route("/fragments/oauth/start", methods=["POST"])
def frag_oauth_start():
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    try:
        auth_url = gmail_client.get_auth_url(state)
        return fragment_response("fragments/oauth_step2.html", {"auth_url": auth_url})
    except FileNotFoundError:
        return _htmx_toast(
            "credentials.json not found. Place your Google OAuth credentials file at /credentials/credentials.json.",
            status=500,
        )
    except Exception as e:
        return _htmx_toast(str(e), status=500)


@app.route("/fragments/oauth/exchange", methods=["POST"])
def frag_oauth_exchange():
    pasted_url = (request.form.get("url") or "").strip()
    if not pasted_url:
        return _htmx_toast("No URL provided.")
    try:
        parsed = urlparse(pasted_url)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
    except Exception:
        return _htmx_toast("Could not parse the URL.")
    if not code:
        return _htmx_toast("No authorization code found in the URL.")
    expected_state = session.get("oauth_state")
    if not expected_state or state != expected_state:
        return _htmx_toast("State mismatch. Please start the authorization process again.")
    try:
        email, credentials_json = gmail_client.exchange_code(state, code)
        db.upsert_account(email, credentials_json)
        db.add_log("INFO", f"Account connected: {email}")
        accounts = _safe_accounts()
        resp = make_response(render_template("fragments/accounts_list.html", accounts=accounts))
        resp.headers["HX-Trigger"] = json.dumps(
            {
                "showToast": {"message": f"{email} connected.", "type": "success"},
                "closeOAuthPanel": True,
            }
        )
        return resp
    except Exception as e:
        db.add_log("ERROR", f"OAuth exchange failed: {e}")
        return _htmx_toast(str(e), status=500)


@app.route("/fragments/scan", methods=["POST"])
def frag_scan():
    db.add_log("INFO", "Manual scan triggered.")
    poller.run_now()
    resp = make_response("", 204)
    resp.headers["HX-Trigger"] = json.dumps(
        {"showToast": {"message": "Scan triggered. Check logs for results.", "type": "success"}}
    )
    return resp


@app.route("/fragments/account-options")
def frag_account_options():
    opt_type = request.args.get("type", "filter")
    first_options = {
        "new-prompt": '<option value="">All accounts (global)</option>',
        "retention": '<option value="">Select an account\u2026</option>',
    }
    first_option = first_options.get(opt_type, '<option value="">All accounts</option>')
    accounts = _safe_accounts()
    return render_template("fragments/account_options.html", first_option=first_option, accounts=accounts)


@app.route("/fragments/retention-query")
def frag_retention_query():
    account_id = request.args.get("account_id", "").strip()
    if not account_id:
        return Response("", content_type="text/html")
    account_id = int(account_id)
    if not db.get_account(account_id):
        return Response("", content_type="text/html")
    return _retention_panel(account_id)


@app.route("/api/prompts/generate-stream")
def api_generate_prompt_stream():
    description = request.args.get("description", "").strip()

    def generate():
        if not description:
            yield "event: done\ndata: \n\n"
            return
        stream = _llm.stream_generate_prompt_instruction(description)
        try:
            for event in stream:
                event_type = event.get("type", "content")
                text = event.get("text", "")
                lines = ["event: " + event_type] + [f"data: {line}" for line in text.split("\n")] + ["", ""]
                yield "\n".join(lines)
            yield "event: done\ndata: \n\n"
        except Exception as e:
            db.add_log("ERROR", f"Prompt generation failed: {e}")
            yield "event: error\ndata: Generation failed. Check Ollama is running.\n\n"
            yield "event: done\ndata: \n\n"
        finally:
            stream.close()

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
    threading.Thread(target=_llm.ensure_model_pulled, daemon=True).start()
    poller.start()
    return app


if __name__ == "__main__":
    application = create_app()
    application.run(host="0.0.0.0", port=5000, debug=False)
