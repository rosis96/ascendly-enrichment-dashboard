"""Reoon Email Verifier integration.

Docs: https://www.reoon.com/articles/api-documentation-of-reoon-email-verifier/

Real calls run when REOON_API_KEY is set. Otherwise a deterministic DEMO verifier
is used so the dashboard works locally with no key and no credit spend.

Single-email endpoint is used per lead (mode=power by default) so the dashboard
can show live progress and Stop mid-run. Bulk task endpoints are included for
later use on very large lists.
"""
import os
import hashlib

try:
    import requests
except Exception:  # requests is optional until real mode is used
    requests = None

BASE = "https://emailverifier.reoon.com/api/v1"


def _key():
    return os.getenv("REOON_API_KEY", "").strip()


def enabled():
    return bool(_key()) and requests is not None


# ------------------------- real API -------------------------

def check_balance():
    r = requests.get(f"{BASE}/check-account-balance/", params={"key": _key()}, timeout=20)
    return r.json()


def verify_single(email, mode="power"):
    r = requests.get(f"{BASE}/verify", params={"email": email, "key": _key(), "mode": mode}, timeout=120)
    return r.json()


def create_bulk_task(emails, name="Dashboard task"):
    r = requests.post(f"{BASE}/create-bulk-verification-task/",
                      json={"name": name[:25], "emails": emails, "key": _key()}, timeout=60)
    return r.json()


def get_bulk_result(task_id):
    r = requests.get(f"{BASE}/get-result-bulk-verification-task/",
                     params={"key": _key(), "task_id": task_id}, timeout=60)
    return r.json()


# ------------------------- demo fallback -------------------------

def demo_verify(email):
    e = (email or "").lower().strip()
    if not e or "@" not in e:
        return {"email": e, "status": "invalid", "is_safe_to_send": False, "verification_mode": "demo"}
    local = e.split("@")[0]
    if local in ("info", "sales", "support", "admin", "contact", "hello", "noreply", "no-reply"):
        return {"email": e, "status": "role_account", "is_role_account": True,
                "is_safe_to_send": False, "verification_mode": "demo"}
    h = int(hashlib.md5(e.encode()).hexdigest(), 16) % 10
    if h < 7:
        return {"email": e, "status": "safe", "is_safe_to_send": True, "is_deliverable": True,
                "verification_mode": "demo"}
    if h < 9:
        return {"email": e, "status": "catch_all", "is_catch_all": True,
                "is_safe_to_send": False, "verification_mode": "demo"}
    return {"email": e, "status": "invalid", "is_safe_to_send": False, "verification_mode": "demo"}


def verify_one(email, mode="power"):
    """Verify a single email, real or demo. Never raises — returns 'unknown' on error."""
    if enabled():
        try:
            return verify_single(email, mode)
        except Exception as exc:
            return {"email": email, "status": "unknown", "error": str(exc), "is_safe_to_send": False}
    return demo_verify(email)


# ------------------------- status bucketing -------------------------

# Outbound-friendly grouping of Reoon statuses.
_VALID = {"safe", "valid"}
_RISKY = {"catch_all", "unknown", "role_account", "inbox_full"}
_INVALID = {"invalid", "disposable", "spamtrap", "disabled"}


def bucket(result):
    """Return (bucket, label): bucket in {'valid','risky','invalid'}; label is the raw status."""
    status = str(result.get("status", "")).lower()
    if result.get("is_safe_to_send") is True or status in _VALID:
        return "valid", status or "valid"
    if status in _RISKY:
        return "risky", status
    if status in _INVALID:
        return "invalid", status
    return "risky", status or "unknown"
