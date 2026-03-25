from app import db, gmail_client


def cleanup_retention(account: dict, service) -> None:
    """Trash emails that exceed per-label or global retention rules."""
    account_id = account["id"]
    email_addr = account["email"]
    try:
        retention = db.get_retention(account_id)
        trashed_ids = set()

        exempt_names = {e["label_name"] for e in retention.get("exemptions", [])}
        exempt_lower = {n.lower() for n in exempt_names}

        for rule in retention["labels"]:
            if rule["label_name"].lower() in exempt_lower:
                continue
            ids = gmail_client.fetch_emails_older_than(service, rule["days"], rule["label_name"])
            new_ids = [i for i in ids if i not in trashed_ids]
            if new_ids:
                gmail_client.batch_trash_emails(service, new_ids)
                trashed_ids.update(new_ids)
                db.add_log(
                    "INFO",
                    f"[{email_addr}] Retention: trashed {len(new_ids)} email(s) with label "
                    f"'{rule['label_name']}' older than {rule['days']} day(s).",
                )

        if retention["global_days"]:
            excluded = list({rule["label_name"] for rule in retention["labels"]} | exempt_names)
            ids = gmail_client.fetch_emails_older_than(service, retention["global_days"], excluded_labels=excluded)
            new_ids = [i for i in ids if i not in trashed_ids]
            if new_ids:
                gmail_client.batch_trash_emails(service, new_ids)
                db.add_log(
                    "INFO",
                    f"[{email_addr}] Retention: trashed {len(new_ids)} email(s) older than "
                    f"{retention['global_days']} day(s) (global rule).",
                )
    except Exception as e:
        db.add_log("ERROR", f"[{email_addr}] Retention cleanup failed: {e}")
