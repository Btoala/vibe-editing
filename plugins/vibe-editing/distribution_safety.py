"""Shared distribution plans, channel histories and approval checks.

Live mutations are deliberately disabled pending account verification and a
separate release review. Existing publishing entrypoints use these checks.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

UTC = timezone.utc


class Blocked(RuntimeError):
    pass


def require_live_publishing():
    """Release gate: no environment variable or command-line flag bypasses it."""
    raise Blocked("Live publishing is disabled pending connected-account verification and explicit release approval.")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def full_hash(path):
    """Hash every byte; names, size and mtime are never duplicate identifiers."""
    path = Path(path)
    before = path.stat()
    if not path.is_file() or before.st_size == 0:
        raise Blocked("Missing or empty media")
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise Blocked("Media changed during hashing")
    return h.hexdigest(), after.st_size


def probe(path):
    try:
        raw = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                             check=True, capture_output=True, text=True, timeout=30)
        data = json.loads(raw.stdout)
        video = next(s for s in data["streams"] if s.get("codec_type") == "video")
        rotation = int(video.get("tags", {}).get("rotate", 0))
        for side in video.get("side_data_list", []):
            rotation = int(side.get("rotation", rotation))
        w, h = int(video["width"]), int(video["height"])
        sar = video.get("sample_aspect_ratio", "1:1")
        if sar not in ("1:1", "N/A", "0:1"):
            raise Blocked("Normalize non-square pixels before distribution")
        if rotation % 180:
            w, h = h, w
        return {"width": w, "height": h, "duration": float(data["format"]["duration"]),
                "audio": any(s.get("codec_type") == "audio" for s in data["streams"])}
    except (KeyError, ValueError, StopIteration, subprocess.SubprocessError) as exc:
        raise Blocked("Media probe failed; do not treat unknown duration as eligible") from exc


def aware(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise Blocked("Schedule must be an ISO timestamp with an explicit UTC offset") from exc
    if result.tzinfo is None:
        raise Blocked("Schedule must include an explicit UTC offset")
    return result.astimezone(UTC)


@dataclass(frozen=True)
class Destination:
    alias: str
    name: str
    youtube_id: str
    route: str
    timezone: str = "America/New_York"
    buffer_id: str = ""
    organization_id: str = ""
    credential_profile: str = "existing-private-credentials"
    daily_cap: int = 10
    youtube_token_path: str = ""
    youtube_client_secret_path: str = ""
    storage_base_url: str = ""
    storage_bucket: str = ""
    storage_prefix: str = "buffer-videos"

    @property
    def key(self):
        # One YouTube destination across both direct and Buffer transports.
        return "youtube:" + self.youtube_id

    def validate(self):
        import re
        if not self.alias or not self.name or not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", self.youtube_id):
            raise Blocked("Explicit alias, name and canonical YouTube channel ID required")
        if self.route not in ("buffer", "youtube"):
            raise Blocked("Unsupported route")
        if self.route == "buffer" and not (self.buffer_id and self.organization_id):
            raise Blocked("Buffer profile and organization IDs required")
        if not self.credential_profile or type(self.daily_cap) is not int or self.daily_cap < 1:
            raise Blocked("Invalid credential profile or daily cap")
        try:
            ZoneInfo(self.timezone)
        except (ValueError, KeyError) as exc:
            raise Blocked("Invalid timezone; no silent fallback to UTC") from exc


def check_configuration(destination, legacy_env_id=None, legacy_file_id=None):
    destination.validate()
    expected = destination.buffer_id if destination.route == "buffer" else destination.youtube_id
    for configured in (legacy_env_id, legacy_file_id):
        if configured is not None and configured != expected:
            raise Blocked("Destination conflicts with existing configuration")


def check_observation(destination, observation, now):
    """Normalized read-only account evidence; fake evidence is used in tests only.

    Production adapters must obtain this directly from provider reads, never from
    user-editable plan JSON. A name match is not ownership verification.
    """
    destination.validate()
    age = now - aware(observation["checked_at"])
    if age < timedelta(0) or age > timedelta(minutes=5):
        raise Blocked("Destination verification is stale")
    if not observation.get("can_write") or not observation.get("name"):
        raise Blocked("Destination access not verified")
    if observation.get("service") != "youtube" or observation.get("youtube_id") != destination.youtube_id:
        raise Blocked("Wrong destination service or channel")
    if destination.route == "buffer":
        if observation.get("buffer_id") != destination.buffer_id or observation.get("organization_id") != destination.organization_id:
            raise Blocked("Wrong Buffer profile or organization")
        if observation.get("disconnected", True) or observation.get("locked", True) or observation.get("paused", True):
            raise Blocked("Buffer channel unavailable or paused")
    elif observation.get("mine_channel_ids") != [destination.youtube_id]:
        raise Blocked("OAuth channel is wrong or ambiguous")


def prepare(path, destination, intent, title, description, *, operation="upload_private",
            visibility="private", due_at=None, remote_id=None, remote_snapshot=None,
            now=None, media_probe=probe, made_for_kids=False, notify_subscribers=False):
    """Build a complete review plan without credentials, network or ledger writes."""
    now = now or datetime.now(UTC)
    destination.validate()
    sha, size = full_hash(path)
    if intent.get("destination") != destination.key:
        raise Blocked("Clip manifest does not name this destination")
    if intent.get("sha256") != sha or not intent.get("asset_id"):
        raise Blocked("Clip manifest hash/asset identity is missing or stale")
    if not isinstance(title, str) or not title.strip() or len(title) > 95 or any(c in title for c in "<>\r\n"):
        raise Blocked("Title must be explicit, valid and at most 95 characters")
    if not isinstance(description, str) or len(description.encode()) > 5000 or any(c in description for c in "<>"):
        raise Blocked("Description exceeds the supported YouTube field")
    if type(made_for_kids) is not bool or type(notify_subscribers) is not bool:
        raise Blocked("Audience and notification settings must be explicit booleans")
    supported = {"buffer": {"buffer_draft", "buffer_schedule", "buffer_promote"}, "youtube": {"upload_private", "youtube_schedule"}}
    if operation not in supported[destination.route]:
        raise Blocked("Operation does not belong to the selected route")
    media = media_probe(path)
    duration = media["duration"]
    if not math.isfinite(duration) or duration <= 0 or media["width"] <= 0 or media["height"] <= 0:
        raise Blocked("Unknown or invalid media duration/dimensions")
    if destination.route == "buffer":
        if not 10 <= duration <= 180 or not media["audio"] or abs(media["width"] / media["height"] - 9 / 16) > 0.02:
            raise Blocked("Buffer shorts require audio, 10–180 seconds and vertical 9:16")
    elif abs(media["width"] / media["height"] - 16 / 9) > 0.06:
        raise Blocked("Existing YouTube mids path requires horizontal 16:9")
    scheduled = operation in ("buffer_schedule", "buffer_promote", "youtube_schedule")
    if visibility not in ("private", "unlisted", "public"):
        raise Blocked("Unsupported visibility")
    if scheduled:
        when = aware(due_at)
        if when <= now + timedelta(minutes=5):
            raise Blocked("Schedule needs at least five minutes of lead time")
        if operation == "youtube_schedule" and visibility != "public":
            raise Blocked("YouTube publishAt means future public publication")
        due_at = when.isoformat().replace("+00:00", "Z")
    elif due_at is not None or visibility != "private":
        raise Blocked("Unscheduled draft/upload must remain private")
    is_update = operation in ("youtube_schedule", "buffer_promote")
    if is_update and (not remote_id or remote_snapshot is None):
        raise Blocked("Scheduling requires a specific adopted video ID and snapshot")
    if not is_update and (remote_id or remote_snapshot is not None):
        raise Blocked("Unexpected remote video on a create operation")
    if full_hash(path) != (sha, size):
        raise Blocked("Media changed during media inspection")
    plan = {"schema": 2, "destination": asdict(destination), "destination_key": destination.key,
            "file": str(Path(path).resolve()), "sha256": sha, "size_bytes": size,
            "asset_id": intent["asset_id"], "media": media, "title": title, "description": description,
            "operation": operation, "visibility": visibility, "due_at": due_at,
            "remote_id": remote_id, "remote_snapshot": copy.deepcopy(remote_snapshot),
            "made_for_kids": made_for_kids, "notify_subscribers": notify_subscribers,
            "category_id": str(intent.get("category_id", "22")),
            "created_at": now.isoformat(), "expires_at": (now + timedelta(hours=1)).isoformat()}
    if operation == "youtube_schedule":
        youtube_schedule_body(plan, remote_snapshot)
    if operation == "buffer_promote":
        buffer_edit_request(plan, remote_snapshot)
    plan["provider_request"] = request_envelope(plan)
    return {**plan, "plan_id": digest(plan)}


def validate_plan(plan, now):
    payload = {k: v for k, v in plan.items() if k != "plan_id"}
    if digest(payload) != plan.get("plan_id"):
        raise Blocked("Plan changed after review")
    if plan.get("schema") != 2 or plan.get("provider_request") != request_envelope(plan):
        raise Blocked("Provider payload differs from the reviewed request")
    if not aware(plan["created_at"]) <= now < aware(plan["expires_at"]):
        raise Blocked("Plan is stale or future-dated")
    if plan["due_at"] and aware(plan["due_at"]) <= now + timedelta(minutes=5):
        raise Blocked("Approved schedule is now too close or in the past")
    sha, size = full_hash(plan["file"])
    if (sha, size) != (plan["sha256"], plan["size_bytes"]):
        raise Blocked("Media changed after review")


def preview(plan, observation=None):
    d = plan["destination"]
    due = plan["due_at"]
    local = aware(due).astimezone(ZoneInfo(d["timezone"])).isoformat() if due else "None"
    current = "private" if plan["operation"] in ("upload_private", "youtube_schedule") else "Buffer draft or pending delivery; no YouTube upload yet"
    # JSON escapes control characters; titles/descriptions are not truncated.
    card = {"mode": "REVIEW — live publishing disabled", "plan_id": plan["plan_id"],
            "destination": {"configured_name": d["name"], "verified_name": (observation or {}).get("name", "UNVERIFIED"),
                            "youtube_channel_id": d["youtube_id"], "route": d["route"],
                            "buffer_profile_id": d["buffer_id"], "buffer_organization_id": d["organization_id"]},
            "video": {k: plan[k] for k in ("file", "sha256", "size_bytes", "asset_id", "remote_id", "media")},
            "title": plan["title"], "description": plan["description"], "operation": plan["operation"],
            "visibility_now": current, "visibility_at_scheduled_delivery": plan["visibility"] if due else "no scheduled publication",
            "schedule_utc": due, "schedule_local": local, "timezone": d["timezone"],
            "made_for_kids": plan["made_for_kids"], "notify_subscribers": plan["notify_subscribers"],
            "storage_exposure": "Publicly fetchable media required before Buffer submission" if d["route"] == "buffer" else "No Buffer media hosting",
            "approval": "Required for this exact plan before any external write"}
    card["provider_request"] = plan["provider_request"]
    if plan["operation"] == "youtube_schedule":
        card["made_for_kids"] = plan["remote_snapshot"]["status"].get("selfDeclaredMadeForKids", "preserve existing")
        card["notify_subscribers"] = "not changed by metadata update"
    elif plan["operation"] == "buffer_promote":
        old = plan["remote_snapshot"].get("metadata") or {}
        card["made_for_kids"] = old.get("madeForKids", "preserve existing")
        card["notify_subscribers"] = old.get("notifySubscribers", "preserve existing")
    return json.dumps(card, indent=2, ensure_ascii=False)


def buffer_request(plan, video_url):
    if plan["operation"] not in ("buffer_draft", "buffer_schedule"):
        raise Blocked("Not a Buffer plan")
    scheduled = plan["operation"] == "buffer_schedule"
    body = {"channelId": plan["destination"]["buffer_id"], "text": plan["description"],
            "schedulingType": "automatic", "mode": "customScheduled" if scheduled else "addToQueue",
            "saveToDraft": not scheduled, "needsApproval": False,
            "assets": [{"video": {"url": video_url}}],
            "metadata": {"youtube": {"title": plan["title"], "categoryId": plan["category_id"], "privacy": plan["visibility"],
                                      "madeForKids": plan["made_for_kids"], "notifySubscribers": plan["notify_subscribers"]}}}
    if scheduled:
        body["dueAt"] = plan["due_at"]
    return {"input": body}  # GraphQL variables, never interpolated strings.


def youtube_upload_body(plan):
    if plan["operation"] != "upload_private":
        raise Blocked("Not a private upload plan")
    return {"snippet": {"title": plan["title"], "description": plan["description"], "categoryId": plan["category_id"]},
            "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": plan["made_for_kids"]}}


def youtube_schedule_body(plan, current):
    if current != plan["remote_snapshot"]:
        raise Blocked("Remote metadata changed after review")
    if current.get("id") != plan["remote_id"] or current.get("snippet", {}).get("channelId") != plan["destination"]["youtube_id"]:
        raise Blocked("Remote video is not the approved video/channel")
    status = current.get("status", {})
    if status.get("privacyStatus") != "private" or status.get("publishAt"):
        raise Blocked("Video is not an unscheduled private draft")
    # Only documented mutable properties. Never round-trip read-only fields.
    snippets = ("title", "description", "categoryId", "tags", "defaultLanguage")
    statuses = ("privacyStatus", "license", "embeddable", "publicStatsViewable", "selfDeclaredMadeForKids", "containsSyntheticMedia")
    snippet = {k: copy.deepcopy(v) for k, v in current["snippet"].items() if k in snippets}
    preserved_status = {k: copy.deepcopy(v) for k, v in status.items() if k in statuses}
    snippet.update(title=plan["title"], description=plan["description"])
    if not snippet.get("categoryId"):
        raise Blocked("Existing category required")
    preserved_status.update(privacyStatus="private", publishAt=plan["due_at"])
    return {"id": plan["remote_id"], "snippet": snippet, "status": preserved_status}


def require_buffer_media_binding(plan, post):
    """Fail closed unless one returned video names the approved hash object.

    If the provider rewrites this source, keep the operation held until a
    separately verified immutable asset mapping can establish equivalence.
    """
    expected = expected_media_url(plan)
    assets = post.get("assets")
    if not expected or not isinstance(assets, list) or len(assets) != 1:
        raise Blocked("Buffer media identity is missing or ambiguous")
    asset = assets[0]
    if (not isinstance(asset, dict) or asset.get("source") != expected
            or not str(asset.get("mimeType", "")).startswith("video/")):
        raise Blocked("Buffer media differs from the approved content identity")


def buffer_edit_request(plan, current):
    if current != plan["remote_snapshot"]:
        raise Blocked("Buffer draft changed after review")
    if current.get("id") != plan["remote_id"] or current.get("channelId") != plan["destination"]["buffer_id"] or current.get("status") != "draft":
        raise Blocked("Not the approved Buffer draft/channel")
    require_buffer_media_binding(plan, current)
    # Edit only explicitly approved metadata; omitted assets remain unchanged.
    return {"input": {"id": plan["remote_id"], "text": plan["description"], "saveToDraft": False,
                      "mode": "customScheduled", "schedulingType": "automatic", "dueAt": plan["due_at"],
                      "metadata": {"youtube": {"title": plan["title"], "privacy": plan["visibility"]}}}}


def expected_media_url(plan):
    from urllib.parse import quote, urlsplit
    d = plan["destination"]
    base, bucket = d["storage_base_url"].rstrip("/"), d["storage_bucket"]
    if not base or not bucket:
        return None  # Preview identifies missing storage configuration; execution rejects it.
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise Blocked("Storage base URL must be an HTTPS origin without credentials")
    object_name = d["storage_prefix"].strip("/") + "/" + plan["sha256"]
    return f"{base}/storage/v1/object/public/{quote(bucket, safe='')}/{quote(object_name, safe='/')}"


def request_envelope(plan):
    operation = plan["operation"]
    if operation in ("buffer_draft", "buffer_schedule"):
        return {"method": "createPost", "variables": buffer_request(plan, expected_media_url(plan))}
    if operation == "buffer_promote":
        return {"method": "editPost", "variables": buffer_edit_request(plan, plan["remote_snapshot"])}
    if operation == "upload_private":
        return {"method": "videos.insert", "part": "snippet,status", "notifySubscribers": plan["notify_subscribers"], "body": youtube_upload_body(plan)}
    if operation == "youtube_schedule":
        body = youtube_schedule_body(plan, plan["remote_snapshot"])
        old = plan["remote_snapshot"]["snippet"]
        if plan["title"] == old.get("title", "") and plan["description"] == old.get("description", ""):
            body.pop("snippet")
            return {"method": "videos.update", "part": "status", "body": body}
        return {"method": "videos.update", "part": "snippet,status", "body": body}
    raise Blocked("Unknown operation")


def stage_media(plan, state_directory):
    """Copy approved bytes into private content-addressed staging before sending."""
    stage = Path(state_directory) / "media"
    stage.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = stage / plan["sha256"]
    if target.exists():
        if full_hash(target) != (plan["sha256"], plan["size_bytes"]):
            raise Blocked("Staged media identity mismatch")
        return target
    fd, temp_name = tempfile.mkstemp(prefix=".stage-", dir=stage)
    try:
        with os.fdopen(fd, "wb") as output, open(plan["file"], "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        temp = Path(temp_name)
        if full_hash(temp) != (plan["sha256"], plan["size_bytes"]):
            raise Blocked("Media changed before staging")
        temp.chmod(0o400)
        os.replace(temp, target)
        return target
    finally:
        Path(temp_name).unlink(missing_ok=True)


def storage_identity(endpoint, bucket, prefix, sha256):
    """Immutable full-hash objects; changed bucket/config cannot reuse old URLs."""
    object_key = prefix.strip("/") + "/" + sha256
    return digest({"endpoint": endpoint.rstrip("/"), "bucket": bucket, "object": object_key}), object_key


def check_prehosted(plan, *, batch_count, observed_sha256, immutable):
    if batch_count != 1 or observed_sha256 != plan["sha256"] or not immutable:
        raise Blocked("Pre-hosted media must be one verified immutable asset matching this hash")


SCHEMA = """
CREATE TABLE operations (
 plan_id TEXT PRIMARY KEY, destination TEXT NOT NULL, sha256 TEXT NOT NULL,
 asset_id TEXT NOT NULL, operation TEXT NOT NULL, status TEXT NOT NULL,
 day TEXT NOT NULL, request_json TEXT NOT NULL, remote_id TEXT, evidence TEXT
);
CREATE UNIQUE INDEX create_hash ON operations(destination, sha256) WHERE operation NOT IN ('youtube_schedule','buffer_promote');
CREATE UNIQUE INDEX create_asset ON operations(destination, asset_id) WHERE operation NOT IN ('youtube_schedule','buffer_promote');
CREATE UNIQUE INDEX schedule_video ON operations(destination, remote_id, operation) WHERE operation IN ('youtube_schedule','buffer_promote');
CREATE TABLE events (plan_id TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE legacy_sources (path TEXT PRIMARY KEY, sha256 TEXT NOT NULL);
CREATE TABLE legacy_holds (source TEXT NOT NULL, record_key TEXT NOT NULL, record_json TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(source,record_key));
CREATE TABLE legacy_usage (day TEXT PRIMARY KEY, count INTEGER NOT NULL);
CREATE TABLE slots (destination TEXT NOT NULL, due_at TEXT NOT NULL, plan_id TEXT UNIQUE NOT NULL, PRIMARY KEY(destination,due_at));
CREATE TABLE sessions (plan_id TEXT PRIMARY KEY, session_uri TEXT NOT NULL);
PRAGMA user_version=2;
"""


class Ledger:
    """One local SQLite database with channel-scoped histories and atomic claims.

    Explicit initialization only. Missing/corrupt stores cannot silently become
    empty histories. Reconciliation never automatically retries an uncertain send.
    """
    def __init__(self, path):
        self.path = Path(path)

    def initialize(self, legacy_paths=()):
        # Inspect and back up before creating a new history. Never infer channels
        # or discard old filename-only entries.
        legacy = []
        for path in legacy_paths:
            path = Path(path).resolve()
            if not path.exists():
                legacy.append((path, "missing", None, None))
                continue
            data = path.read_bytes()
            try:
                if path.suffix == ".jsonl":
                    values = {str(i): json.loads(line) for i, line in enumerate(data.decode().splitlines()) if line.strip()}
                else:
                    values = json.loads(data)
                if not isinstance(values, dict):
                    raise ValueError("not an object")
            except (ValueError, UnicodeError) as exc:
                raise Blocked("Corrupt legacy history must be reconciled before initialization") from exc
            legacy.append((path, hashlib.sha256(data).hexdigest(), values, data))
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Build privately, then link atomically without replacing existing state.
        # An interrupted migration must never expose a valid but empty ledger.
        fd, name = tempfile.mkstemp(prefix=".history-init-", dir=self.path.parent)
        os.close(fd)
        try:
            with closing(sqlite3.connect(name)) as con:
                con.execute("PRAGMA synchronous=FULL")
                con.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
                for path, sha, values, data in legacy:
                    con.execute("INSERT INTO legacy_sources VALUES (?,?)", (str(path), sha))
                    if values is None:
                        continue
                    backup = self.path.parent / "legacy-backup" / (sha + "-" + path.name)
                    backup.parent.mkdir(mode=0o700, exist_ok=True)
                    backup.write_bytes(data)
                    backup.chmod(0o600)
                    for key, value in values.items():
                        if path.name == "api_calls.json" and type(value) is int and value >= 0:
                            con.execute("INSERT INTO legacy_usage VALUES (?,?) ON CONFLICT(day) DO UPDATE SET count=count+excluded.count", (key, value))
                        elif path.name != "url_cache.json":
                            con.execute("INSERT INTO legacy_holds(source,record_key,record_json) VALUES (?,?,?)", (str(path), key, canonical(value)))
                con.commit()
            os.link(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)

    def connect(self, readonly=False):
        try:
            con = sqlite3.connect(self.path.resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=10)
            con.row_factory = sqlite3.Row
            if con.execute("PRAGMA quick_check").fetchone()[0] != "ok" or con.execute("PRAGMA user_version").fetchone()[0] != 2:
                con.close()
                raise Blocked("Invalid ledger; reconcile/restore before continuing")
            con.execute("PRAGMA synchronous=FULL")
            return con
        except sqlite3.DatabaseError as exc:
            raise Blocked("Missing or corrupt ledger; do not reset history") from exc

    def history(self, destination):
        with closing(self.connect(readonly=True)) as con:
            return [dict(r) for r in con.execute("SELECT * FROM operations WHERE destination=? ORDER BY rowid", (destination,))]

    def reserve(self, plan, now, global_cap=10):
        with closing(self.connect()) as con:
            self._reserve_in(con, plan, now, global_cap)

    def preflight(self, plans, now, global_cap=10):
        """Check the whole batch without writing the authoritative database.

        A snapshot is advisory; each real reservation still rechecks atomically.
        """
        with closing(self.connect(readonly=True)) as source, closing(sqlite3.connect(":memory:")) as preview:
            preview.row_factory = sqlite3.Row
            source.backup(preview)
            for plan in plans:
                self._reserve_in(preview, plan, now, global_cap)

    def _reserve_in(self, con, plan, now, global_cap=10):
        if type(global_cap) is not int or global_cap < 1:
            raise Blocked("Invalid global cap")
        day = now.astimezone(UTC).date().isoformat()
        try:
            con.execute("BEGIN IMMEDIATE")
            for source in con.execute("SELECT * FROM legacy_sources"):
                p = Path(source["path"])
                sha = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "missing"
                if sha != source["sha256"]:
                    raise Blocked("Legacy history changed; stop old publishers and reconcile")
            if con.execute("SELECT 1 FROM legacy_holds WHERE resolved=0 LIMIT 1").fetchone():
                raise Blocked("Unresolved legacy history blocks distribution")
            if con.execute("SELECT 1 FROM operations WHERE plan_id=?", (plan["plan_id"],)).fetchone():
                raise Blocked("Repeated submission; inspect existing operation")
            legacy_used = con.execute("SELECT COALESCE(SUM(count),0) FROM legacy_usage WHERE day=?", (day,)).fetchone()[0]
            if con.execute("SELECT COUNT(*) FROM operations WHERE day=?", (day,)).fetchone()[0] + legacy_used >= global_cap:
                raise Blocked("Global daily attempt cap reached")
            if con.execute("SELECT COUNT(*) FROM operations WHERE day=? AND destination=?", (day, plan["destination_key"])).fetchone()[0] >= plan["destination"]["daily_cap"]:
                raise Blocked("Channel daily attempt cap reached")
            if plan["operation"] in ("youtube_schedule", "buffer_promote"):
                previous = ("upload_private", "adopt_youtube") if plan["operation"] == "youtube_schedule" else ("buffer_draft", "adopt_buffer")
                prior = con.execute("SELECT * FROM operations WHERE destination=? AND sha256=? AND status='confirmed' AND operation IN (?,?) AND remote_id=?",
                                    (plan["destination_key"], plan["sha256"], *previous, plan["remote_id"])).fetchone()
                if not prior:
                    raise Blocked("Draft identity needs a confirmed upload or explicit legacy adoption")
            if plan["due_at"]:
                con.execute("INSERT INTO slots VALUES (?,?,?)", (plan["destination_key"], plan["due_at"], plan["plan_id"]))
            con.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?)", (
                plan["plan_id"], plan["destination_key"], plan["sha256"], plan["asset_id"], plan["operation"],
                "submitting", day, canonical(plan), plan["remote_id"], None))
            con.execute("INSERT INTO events VALUES (?,?,?)", (plan["plan_id"], "submitting", "Atomic reservation before provider call"))
            con.commit()
        except sqlite3.IntegrityError as exc:
            con.rollback()
            raise Blocked("Duplicate content, asset lineage or remote video for this channel") from exc
        except BaseException:
            con.rollback()
            raise

    def finish(self, plan_id, status, remote_id=None, evidence=""):
        if status not in ("confirmed", "unknown"):
            raise Blocked("Unsupported transition")
        with closing(self.connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            changed = con.execute("UPDATE operations SET status=?,remote_id=COALESCE(?,remote_id),evidence=? WHERE plan_id=? AND status IN ('submitting','unknown')",
                                  (status, remote_id, evidence, plan_id)).rowcount
            if changed != 1:
                con.rollback()
                raise Blocked("No eligible pending operation")
            con.execute("INSERT INTO events VALUES (?,?,?)", (plan_id, status, evidence))
            con.commit()

    def reconcile_confirmed(self, plan, receipt, evidence):
        if not evidence:
            raise Blocked("Reconciliation evidence required")
        check_receipt(plan, receipt)
        self.finish(plan["plan_id"], "confirmed", receipt["id"], evidence)

    def save_session(self, plan_id, uri):
        # Session URIs are sensitive. Keep them in this mode-0600 database only.
        from urllib.parse import urlsplit
        parsed = urlsplit(uri)
        if parsed.scheme != "https" or parsed.hostname != "www.googleapis.com":
            raise Blocked("Unexpected resumable upload session origin")
        with closing(self.connect()) as con:
            if not con.execute("SELECT 1 FROM operations WHERE plan_id=? AND status IN ('submitting','unknown')", (plan_id,)).fetchone():
                raise Blocked("Upload session lacks a pending operation")
            con.execute("INSERT INTO sessions VALUES (?,?) ON CONFLICT(plan_id) DO UPDATE SET session_uri=excluded.session_uri", (plan_id, uri))
            con.commit()

    def require_pending(self, plan):
        with closing(self.connect(readonly=True)) as con:
            row = con.execute("SELECT request_json,status FROM operations WHERE plan_id=?", (plan["plan_id"],)).fetchone()
            if row is None or row["status"] != "submitting" or row["request_json"] != canonical(plan):
                raise Blocked("Provider write requires the exact pending ledger reservation")

    def record_remote_id(self, plan_id, remote_id):
        if not remote_id:
            raise Blocked("Remote ID missing")
        with closing(self.connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT remote_id,status FROM operations WHERE plan_id=?", (plan_id,)).fetchone()
            if row is None or row["status"] != "submitting" or row["remote_id"] not in (None, remote_id):
                con.rollback()
                raise Blocked("Remote ID conflicts with pending operation")
            con.execute("UPDATE operations SET remote_id=? WHERE plan_id=?", (remote_id, plan_id))
            con.commit()

    def adopt(self, destination, sha256, asset_id, remote, evidence, legacy_source=None, legacy_key=None):
        """Explicit human mapping plus trusted read-only provider ownership proof.

        Providers do not expose the original full-file hash. This is a recorded
        human attestation of content identity, never automatic hash verification.
        """
        destination.validate()
        if (legacy_source is None) != (legacy_key is None):
            raise Blocked("Legacy source and key must be specified together")
        owner = remote.get("snippet", {}).get("channelId") if destination.route == "youtube" else remote.get("channelId")
        expected = destination.youtube_id if destination.route == "youtube" else destination.buffer_id
        import re
        if owner != expected or not remote.get("id") or not evidence.strip() or not asset_id or not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise Blocked("Legacy adoption needs verified ownership and an explicit content mapping")
        record = {"destination": destination.key, "sha256": sha256, "asset_id": asset_id,
                  "remote_id": remote["id"], "snapshot": remote, "evidence": evidence,
                  "identity_basis": "human-confirmed mapping; provider verified ownership only"}
        operation_id = digest(record)
        with closing(self.connect()) as con:
            try:
                con.execute("BEGIN IMMEDIATE")
                allowed = ("upload_private", "adopt_youtube") if destination.route == "youtube" else ("buffer_draft", "buffer_schedule", "adopt_buffer")
                markers = ",".join("?" for _ in allowed)
                existing = con.execute(
                    "SELECT plan_id FROM operations WHERE destination=? AND sha256=? AND asset_id=? "
                    "AND remote_id=? AND status='confirmed' AND operation IN (" + markers + ")",
                    (destination.key, sha256, asset_id, remote["id"], *allowed)).fetchone()
                if existing is not None and legacy_source is not None:
                    # Resolve another reviewed legacy record against the same
                    # confirmed identity; never overwrite history or clear broadly.
                    operation_id = existing["plan_id"]
                else:
                    con.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?)", (
                        operation_id, destination.key, sha256, asset_id, "adopt_" + destination.route,
                        "confirmed", "imported", canonical(record), remote["id"], evidence))
                if legacy_source is not None or legacy_key is not None:
                    changed = con.execute("UPDATE legacy_holds SET resolved=1 WHERE source=? AND record_key=? AND resolved=0",
                                          (str(Path(legacy_source).resolve()), legacy_key)).rowcount
                    if changed != 1:
                        raise Blocked("Legacy source/key does not identify one unresolved record")
                con.execute("INSERT INTO events VALUES (?,?,?)", (operation_id, "adopted", evidence))
                con.commit()
            except sqlite3.IntegrityError as exc:
                con.rollback()
                raise Blocked("Existing content/asset history must be reconciled, not overwritten") from exc
            except BaseException:
                con.rollback()
                raise

    def session(self, plan_id):
        with closing(self.connect(readonly=True)) as con:
            row = con.execute("SELECT session_uri FROM sessions WHERE plan_id=?", (plan_id,)).fetchone()
            return row[0] if row else None

    def summary(self, destination):
        with closing(self.connect(readonly=True)) as con:
            return {"channel_operations": len(self.history(destination)),
                    "unresolved_legacy_records": con.execute("SELECT COUNT(*) FROM legacy_holds WHERE resolved=0").fetchone()[0]}


def check_receipt(plan, receipt):
    # Synthetic contract. Real providers need adapters + readback to establish
    # these facts; neither provider is assumed to return a content hash itself.
    if not isinstance(receipt, dict) or not receipt.get("id"):
        raise Blocked("Provider result is unconfirmed")
    for key, expected in (("destination", plan["destination_key"]), ("sha256", plan["sha256"]),
                          ("plan_id", plan["plan_id"]), ("due_at", plan["due_at"]), ("visibility", plan["visibility"])):
        if receipt.get(key) != expected:
            raise Blocked("Provider result differs from the approved request")
    if plan["remote_id"] and receipt["id"] != plan["remote_id"]:
        raise Blocked("Provider returned a different video ID")


def exercise(plan, destination, observation, ledger, provider, *, approve_id=None, dry_run=True,
             now=None, legacy_env_id=None, legacy_file_id=None, global_cap=10, emit=print):
    """Common submission gate; adapters must also enforce require_live_publishing.

    The approval digest binds values, not human identity. The calling UI must
    capture actual authorization. Tests replace the gate and inject fake I/O.
    """
    now = now or datetime.now(UTC)
    validate_plan(plan, now)
    check_configuration(destination, legacy_env_id, legacy_file_id)
    if asdict(destination) != plan["destination"]:
        raise Blocked("Destination configuration changed after review")
    emit(preview(plan, observation))
    if dry_run:
        return "preview"
    if approve_id != plan["plan_id"]:
        raise Blocked("Explicit approval of this exact plan is required")
    require_live_publishing()
    check_observation(destination, observation, now)
    ledger.reserve(plan, now, global_cap)
    try:
        receipt = provider(plan)
        check_receipt(plan, receipt)
        ledger.finish(plan["plan_id"], "confirmed", receipt["id"], "Provider acknowledgement of approved request; publication status separate")
        return receipt
    except BaseException:
        # A hard process/power loss leaves 'submitting'. Both states block repeats.
        try:
            ledger.finish(plan["plan_id"], "unknown", evidence="Unconfirmed outcome; reconcile before another attempt")
        except Exception:
            pass  # Never mask the original error; persisted reservation still blocks.
        raise


def main():
    parser = argparse.ArgumentParser(description="Offline review only; no upload/queue/schedule/publish commands")
    parser.add_argument("--plan", required=True, type=Path)
    args = parser.parse_args()
    print(preview(json.loads(args.plan.read_text())))


if __name__ == "__main__":
    main()
