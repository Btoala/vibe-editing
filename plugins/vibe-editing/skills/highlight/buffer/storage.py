"""Immutable Supabase hosting for an approved pending distribution operation."""
import hashlib
import mimetypes
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import distribution_safety as safety
from config import storage_bucket, storage_key, storage_url, STORAGE_PREFIX


def verify_public_media(url, expected_sha, allow_missing=False):
    import requests
    with requests.get(url, stream=True, allow_redirects=False, timeout=(10, 120)) as response:
        if allow_missing and response.status_code == 404:
            return False
        if response.status_code != 200:
            raise safety.Blocked("Cannot verify the exact public media bytes")
        h = hashlib.sha256()
        for block in response.iter_content(1024 * 1024):
            h.update(block)
        if h.hexdigest() != expected_sha:
            raise safety.Blocked("Hosted media does not match the approved full-file hash")
    return True


def upload_video(local_path, object_name=None, *, plan=None, ledger=None):
    safety.require_live_publishing()
    if plan is None or ledger is None or object_name is not None:
        raise safety.Blocked("Storage requires an approved pending plan; custom object names are not allowed")
    ledger.require_pending(plan)
    if safety.full_hash(local_path) != (plan["sha256"], plan["size_bytes"]):
        raise safety.Blocked("Upload media changed")
    d = plan["destination"]
    if (storage_url(), storage_bucket(), STORAGE_PREFIX.strip("/")) != (d["storage_base_url"].rstrip("/"), d["storage_bucket"], d["storage_prefix"].strip("/")):
        raise safety.Blocked("Storage configuration differs from the reviewed plan")
    public_url = safety.expected_media_url(plan)
    if not public_url:
        raise safety.Blocked("Explicit storage destination required")
    if verify_public_media(public_url, plan["sha256"], allow_missing=True):
        return public_url
    import requests
    object_path = d["storage_prefix"].strip("/") + "/" + plan["sha256"]
    endpoint = d["storage_base_url"].rstrip("/") + "/storage/v1/object/" + quote(d["storage_bucket"], safe="") + "/" + quote(object_path, safe="/")
    key = storage_key()
    mime = mimetypes.guess_type(plan["file"])[0] or "video/mp4"
    with open(local_path, "rb") as file:
        response = requests.post(endpoint, headers={"Authorization": f"Bearer {key}", "apikey": key,
                                 "Content-Type": mime, "x-upsert": "false"}, data=file, timeout=(10, 120), allow_redirects=False)
    if response.status_code not in (200, 201, 409):
        raise safety.Blocked(f"Storage HTTP {response.status_code}; reconcile before retry")
    verify_public_media(public_url, plan["sha256"])
    return public_url


if __name__ == "__main__":
    try:
        safety.require_live_publishing()
    except safety.Blocked as exc:
        raise SystemExit(str(exc))
