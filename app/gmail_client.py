import base64
import datetime
import json
import os
import time

from cachetools import TTLCache
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app import db
from app.config import EMAIL_BODY_TRUNCATION, GMAIL_LOOKBACK_HOURS, GMAIL_MAX_RESULTS

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "/credentials/credentials.json")
REDIRECT_URI = "http://localhost"

_label_cache: TTLCache = TTLCache(maxsize=32, ttl=300)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, HttpError):
        return exc.resp.status in (429, 500, 502, 503)
    return isinstance(exc, (OSError, ConnectionError, TimeoutError))


_gmail_retry = retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)


def get_auth_url(state: str) -> str:
    flow = Flow.from_client_secrets_file(CREDENTIALS_FILE, scopes=SCOPES, state=state)
    flow.redirect_uri = REDIRECT_URI
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=state,
    )
    return auth_url


def exchange_code(state: str, code: str) -> tuple[str, str]:
    flow = Flow.from_client_secrets_file(CREDENTIALS_FILE, scopes=SCOPES, state=state)
    flow.redirect_uri = REDIRECT_URI
    flow.fetch_token(code=code)
    creds = flow.credentials
    email = _get_email(creds)
    return email, creds.to_json()


def _get_email(creds: Credentials) -> str:
    svc = build("oauth2", "v2", credentials=creds)
    return svc.userinfo().get().execute()["email"]


def get_service(credentials_json: str):
    """Load and refresh credentials. Returns (service, refreshed_json_or_None)."""
    creds = Credentials.from_authorized_user_info(json.loads(credentials_json), SCOPES)
    refreshed = None
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        refreshed = creds.to_json()
    service = build("gmail", "v1", credentials=creds)
    service._cache_key = creds.refresh_token
    return service, refreshed


def get_service_and_refresh(account: dict):
    """Load/refresh credentials, persist if refreshed, return the Gmail service."""
    service, refreshed = get_service(account["credentials_json"])
    if refreshed is not None:
        db.update_account_credentials(account["id"], refreshed)
    return service


def _cache_key(service) -> str:
    return service._cache_key


def build_label_cache(service, label_names: list) -> dict:
    """Return {name: id} for the given label names, creating any that are missing."""
    all_labels = list_labels(service)
    existing = {lbl["name"].lower(): lbl["id"] for lbl in all_labels}
    cache = {}
    for name in label_names:
        if name.lower() in existing:
            cache[name] = existing[name.lower()]
        else:
            try:
                created = (
                    service.users()
                    .labels()
                    .create(
                        userId="me",
                        body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
                    )
                    .execute()
                )
                cache[name] = created["id"]
                _label_cache.pop(_cache_key(service), None)
            except Exception as e:
                db.add_log("WARNING", f"Could not create label '{name}': {e}")
    return cache


@_gmail_retry
def list_recent_message_ids(service, max_results=GMAIL_MAX_RESULTS, lookback_hours=GMAIL_LOOKBACK_HOURS) -> list:
    after_ts = int(time.time() - lookback_hours * 3600)
    ids = []
    request = service.users().messages().list(userId="me", maxResults=max_results, q=f"in:inbox after:{after_ts}")
    while request is not None:
        response = request.execute()
        ids.extend(m["id"] for m in response.get("messages", []))
        request = service.users().messages().list_next(request, response)
    return ids


@_gmail_retry
def _execute_batch(batch) -> None:
    batch.execute()


def fetch_message_details(service, message_ids: list) -> list:
    if not message_ids:
        return []
    results = {}

    def _callback(request_id, response, exception):
        if exception is None:
            results[request_id] = response
        else:
            db.add_log("WARNING", f"Batch fetch failed for message {request_id}: {exception}")

    for i in range(0, len(message_ids), 100):
        batch = service.new_batch_http_request(callback=_callback)
        for msg_id in message_ids[i : i + 100]:
            batch.add(
                service.users().messages().get(userId="me", id=msg_id, format="full"),
                request_id=msg_id,
            )
        _execute_batch(batch)

    emails = []
    for msg_id in message_ids:
        full = results.get(msg_id)
        if not full:
            continue
        headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}
        body = _extract_body(full["payload"])
        emails.append(
            {
                "id": msg_id,
                "subject": headers.get("Subject", "(no subject)"),
                "sender": headers.get("From", "unknown"),
                "snippet": full.get("snippet", ""),
                "body": body[:EMAIL_BODY_TRUNCATION],
            }
        )
    return emails


@_gmail_retry
def list_labels(service) -> list:
    key = _cache_key(service)
    if key in _label_cache:
        return _label_cache[key]
    result = service.users().labels().list(userId="me").execute()
    labels = sorted(
        [{"id": lbl["id"], "name": lbl["name"]} for lbl in result.get("labels", [])],
        key=lambda x: x["name"].lower(),
    )
    _label_cache[key] = labels
    return labels


def fetch_emails_older_than(service, days: int, label_name: str = None, excluded_labels: list = None) -> list:
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    query = f"before:{cutoff.strftime('%Y/%m/%d')}"
    if label_name:
        query += f" label:{label_name}"
    if excluded_labels:
        for lbl in excluded_labels:
            query += f" -label:{lbl}"
    ids = []
    request = service.users().messages().list(userId="me", q=query, maxResults=500)
    while request is not None:
        response = request.execute()
        ids.extend(m["id"] for m in response.get("messages", []))
        request = service.users().messages().list_next(request, response)
    return ids


@_gmail_retry
def batch_modify_emails(service, modifications: list) -> None:
    """Apply label modifications using batchModify. Groups by identical add/remove combos."""
    if not modifications:
        return
    groups: dict = {}
    for message_id, add_labels, remove_labels in modifications:
        key = (tuple(sorted(add_labels)), tuple(sorted(remove_labels)))
        groups.setdefault(key, []).append(message_id)
    for (add_labels, remove_labels), message_ids in groups.items():
        for i in range(0, len(message_ids), 1000):
            body: dict = {"ids": message_ids[i : i + 1000]}
            if add_labels:
                body["addLabelIds"] = list(add_labels)
            if remove_labels:
                body["removeLabelIds"] = list(remove_labels)
            service.users().messages().batchModify(userId="me", body=body).execute()


def batch_trash_emails(service, message_ids: list) -> int:
    if not message_ids:
        return 0
    batch_modify_emails(service, [(mid, ["TRASH"], ["INBOX"]) for mid in message_ids])
    return len(message_ids)


def _extract_body(payload) -> str:
    if "parts" in payload:
        for part in payload["parts"]:
            if part["mimeType"] == "text/plain":
                data = part["body"].get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
        for part in payload["parts"]:
            result = _extract_body(part)
            if result:
                return result
    else:
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    return ""
