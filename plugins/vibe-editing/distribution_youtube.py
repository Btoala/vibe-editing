"""YouTube adapter: reuse private OAuth; verify channel; gated mutations.

Read-only verification never starts a login flow or rewrites a token. A missing
grant is reported for explicit setup. No account operations occur on import.
"""
from __future__ import annotations

import json
import mimetypes
from datetime import datetime, timezone
from pathlib import Path

import distribution_safety as safety

ROOT = Path(__file__).resolve().parent


def get_service(client_secret=None, token_path=None):
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise safety.Blocked("Install the existing YouTube dependencies: google-api-python-client and google-auth-oauthlib") from exc
    token_path = Path(token_path or ROOT / "config/youtube_token.json").expanduser()
    if not token_path.is_file():
        raise safety.Blocked("Existing YouTube OAuth token is missing; complete explicit connection setup first")
    # Use granted scopes from the saved token. Do not overwrite an upload token
    # by pretending it grants force-ssl, or silently sign in as another channel.
    creds = Credentials.from_authorized_user_file(str(token_path))
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())  # In-memory refresh only: no token-file write in verification.
        else:
            raise safety.Blocked("YouTube OAuth grant requires explicit reconnection")
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    service._vibe_scopes = tuple(creds.granted_scopes or creds.scopes or ())
    return service


def owned_channel(svc, expected):
    items, cursor, seen = [], None, set()
    while True:
        response = svc.channels().list(part="id,snippet,contentDetails", mine=True, maxResults=50, pageToken=cursor).execute()
        items.extend(response.get("items", []))
        cursor = response.get("nextPageToken")
        if not cursor:
            break
        if cursor in seen:
            raise safety.Blocked("YouTube channel pagination is incomplete")
        seen.add(cursor)
    if len(items) != 1 or items[0].get("id") != expected:
        raise safety.Blocked("YouTube OAuth channel is wrong or ambiguous")
    return items[0]


def read_video(svc, video_id, channel_id):
    response = svc.videos().list(part="snippet,status", id=video_id).execute()
    items = response.get("items", [])
    if len(items) != 1 or items[0].get("id") != video_id or items[0].get("snippet", {}).get("channelId") != channel_id:
        raise safety.Blocked("Video is missing or belongs to a different channel")
    return items[0]


def list_uploads(svc, channel_id):
    ch = owned_channel(svc, channel_id)
    uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    result, cursor, seen = [], None, set()
    while True:
        page = svc.playlistItems().list(part="contentDetails", playlistId=uploads, maxResults=50, pageToken=cursor).execute()
        ids = [x["contentDetails"]["videoId"] for x in page.get("items", [])]
        if ids:
            videos = svc.videos().list(part="snippet,status", id=",".join(ids)).execute()
            for video in videos.get("items", []):
                if video.get("snippet", {}).get("channelId") != channel_id:
                    raise safety.Blocked("Uploads inventory contains an unexpected channel")
                result.append(video)
        cursor = page.get("nextPageToken")
        if not cursor:
            return result
        if cursor in seen:
            raise safety.Blocked("YouTube pagination repeated; inventory incomplete")
        seen.add(cursor)


def local_receipt(plan, remote_id):
    # Hash is our approved input identity, not a hash claimed to come from YouTube.
    return {"id": remote_id, "destination": plan["destination_key"], "sha256": plan["sha256"],
            "plan_id": plan["plan_id"], "due_at": plan["due_at"], "visibility": plan["visibility"]}


def verify_readback(plan, video):
    if video.get("snippet", {}).get("channelId") != plan["destination"]["youtube_id"]:
        raise safety.Blocked("YouTube result is on another channel")
    if video.get("snippet", {}).get("title") != plan["title"] or video.get("snippet", {}).get("description", "") != plan["description"]:
        raise safety.Blocked("YouTube metadata differs from the approved plan")
    status = video.get("status", {})
    if status.get("privacyStatus") != "private":
        raise safety.Blocked("YouTube result is not private")
    actual_due = status.get("publishAt")
    if bool(actual_due) != bool(plan["due_at"]) or (actual_due and safety.aware(actual_due) != safety.aware(plan["due_at"])):
        raise safety.Blocked("YouTube schedule differs from the approved plan")
    for part in ("snippet", "status"):
        for key, expected in plan["provider_request"]["body"].get(part, {}).items():
            if key != "publishAt" and video.get(part, {}).get(key) != expected:
                raise safety.Blocked("YouTube readback changed a reviewed metadata field")


class YouTubeProvider:
    def __init__(self, destination, service=None):
        self.destination = destination
        self._service = service

    @property
    def svc(self):
        if self._service is None:
            self._service = get_service(self.destination.youtube_client_secret_path, self.destination.youtube_token_path)
        return self._service

    def observation(self):
        ch = owned_channel(self.svc, self.destination.youtube_id)
        scopes = getattr(self.svc, "_vibe_scopes", ())
        if not isinstance(scopes, (list, tuple, set)):
            scopes = ()
        allowed = {"https://www.googleapis.com/auth/youtube", "https://www.googleapis.com/auth/youtube.force-ssl", "https://www.googleapis.com/auth/youtube.upload"}
        return {"checked_at": datetime.now(timezone.utc).isoformat(), "name": ch["snippet"]["title"],
                "service": "youtube", "youtube_id": ch["id"], "mine_channel_ids": [ch["id"]],
                "can_write": bool(set(scopes) & allowed)}

    def occupied_slots(self):
        return [v["status"]["publishAt"] for v in list_uploads(self.svc, self.destination.youtube_id) if v.get("status", {}).get("publishAt")]

    def submit(self, plan, staged, ledger):
        safety.require_live_publishing()
        ledger.require_pending(plan)
        safety.validate_plan(plan, datetime.now(timezone.utc))
        safety.check_observation(self.destination, self.observation(), datetime.now(timezone.utc))
        envelope = plan["provider_request"]
        if plan["operation"] == "youtube_schedule":
            scopes = set(getattr(self.svc, "_vibe_scopes", ()))
            if not scopes & {"https://www.googleapis.com/auth/youtube", "https://www.googleapis.com/auth/youtube.force-ssl"}:
                raise safety.Blocked("The existing OAuth grant cannot update video metadata")
            current = read_video(self.svc, plan["remote_id"], self.destination.youtube_id)
            safety.youtube_schedule_body(plan, current)
            request = self.svc.videos().update(part=envelope["part"], body=envelope["body"])
            # If-Match support must be confirmed before releasing the gate. A
            # precondition failure is held, never retried as a new operation.
            if current.get("etag"):
                request.headers["If-Match"] = current["etag"]
            response = request.execute()
        elif plan["operation"] == "upload_private":
            response = self._upload(plan, staged, ledger)
        else:
            raise safety.Blocked("Unsupported YouTube operation")
        remote_id = response.get("id")
        if not remote_id or (plan["remote_id"] and remote_id != plan["remote_id"]):
            raise safety.Blocked("YouTube result lacks the expected video ID")
        ledger.record_remote_id(plan["plan_id"], remote_id)
        actual = read_video(self.svc, remote_id, self.destination.youtube_id)
        verify_readback(plan, actual)
        return local_receipt(plan, remote_id)

    def _upload(self, plan, staged, ledger):
        safety.require_live_publishing()
        ledger.require_pending(plan)
        from googleapiclient.http import MediaFileUpload
        if safety.full_hash(staged) != (plan["sha256"], plan["size_bytes"]):
            raise safety.Blocked("Staged upload changed")
        envelope = plan["provider_request"]
        media = MediaFileUpload(str(staged), mimetype=mimetypes.guess_type(plan["file"])[0] or "video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
        request = self.svc.videos().insert(part=envelope["part"], body=envelope["body"],
                                         notifySubscribers=envelope["notifySubscribers"], media_body=media)
        prior = ledger.session(plan["plan_id"])
        if prior:
            # No automatic second insert. A recovery invocation can resume this
            # exact URI after reconciliation of the held operation.
            request.resumable_uri = prior
            request._in_error_state = True
        http = request.http
        original_request = http.request
        def recording_request(uri, method="GET", *args, **kwargs):
            response, content = original_request(uri, method, *args, **kwargs)
            # Persist Location before googleapiclient transmits the first chunk.
            # If this write fails the session is held and no chunk is sent.
            location = response.get("location")
            if method == "POST" and location:
                ledger.save_session(plan["plan_id"], location)
            return response, content
        http.request = recording_request
        try:
            result = None
            while result is None:
                _, result = request.next_chunk(num_retries=0)
            return result
        finally:
            http.request = original_request
