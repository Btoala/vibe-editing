"""Buffer adapter with explicit channel verification and gated mutations."""
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import distribution_safety as safety
from config import BUFFER_API_ENDPOINT, buffer_token


class BufferError(safety.Blocked):
    pass


def _gql(query, variables=None):
    if re.search(r"\bmutation\b", query):
        safety.require_live_publishing()
    if BUFFER_API_ENDPOINT.rstrip("/") != "https://api.buffer.com":
        raise BufferError("Unexpected Buffer API endpoint; credentials were not sent")
    import requests
    response = requests.post(BUFFER_API_ENDPOINT, headers={"Content-Type": "application/json", "Authorization": f"Bearer {buffer_token()}"},
                             json={"query": query, "variables": variables or {}}, timeout=(10, 60), allow_redirects=False)
    if response.status_code != 200:
        raise BufferError(f"Buffer HTTP {response.status_code}; outcome requires reconciliation")
    result = response.json()
    if result.get("errors") or not isinstance(result.get("data"), dict):
        raise BufferError("Buffer GraphQL request failed; do not blindly retry mutations")
    return result["data"]


def get_account():
    return _gql("query { account { id organizations { id name } } }")["account"]


def get_channels(org_id):
    return _gql("""query Channels($input: ChannelsInput!) {
      channels(input: $input) {
        id name service serviceId organizationId allowedActions timezone
        isDisconnected isLocked isQueuePaused
      }
    }""", {"input": {"organizationId": org_id}})["channels"]


POST_FIELDS = """id channelId dueAt status text
 metadata { ... on YoutubePostMetadata { title privacy madeForKids notifySubscribers category { categoryId } } }
 assets { id mimeType source }
"""


def get_post(org_id, post_id):
    return _gql("query Post($input: PostInput!) { post(input: $input) { " + POST_FIELDS + " } }",
                {"input": {"id": post_id}})["post"]


def list_posts(org_id, channel_id):
    result, cursor, seen = [], None, set()
    while True:
        query = "query Posts($input: PostsInput!, $after: String) { posts(input: $input, first: 100, after: $after) { edges { node { " + POST_FIELDS + " } } pageInfo { hasNextPage endCursor } } }"
        page = _gql(query, {"input": {"organizationId": org_id, "filter": {"channelIds": [channel_id]}}, "after": cursor})["posts"]
        for edge in page["edges"]:
            node = edge["node"]
            if node.get("channelId") != channel_id:
                raise BufferError("Buffer inventory contains an unexpected channel")
            result.append(node)
        if not page["pageInfo"]["hasNextPage"]:
            return result
        cursor = page["pageInfo"]["endCursor"]
        if not cursor or cursor in seen:
            raise BufferError("Buffer pagination incomplete")
        seen.add(cursor)


def create_video_post(*args, **kwargs):
    """Close the old unreviewed helper; use fill_queue's saved plan."""
    safety.require_live_publishing()
    raise safety.Blocked("Direct creation is unsupported; use a verified manifest-bound plan")


def discover():
    for org in get_account()["organizations"]:
        print(f"Organization: {org['name']} ({org['id']})")
        for ch in get_channels(org["id"]):
            print(f"{ch['service']} | {ch['name']} | Buffer={ch['id']} | external={ch['serviceId']}")
    print("Bind both the Buffer ID and external YouTube ID in the destination registry.")


def _mutate(envelope):
    safety.require_live_publishing()
    method = envelope["method"]
    if method not in ("createPost", "editPost"):
        raise BufferError("Unsupported mutation")
    input_type = "CreatePostInput" if method == "createPost" else "EditPostInput"
    query = "mutation ApprovedPost($input: " + input_type + "!) { " + method + "(input: $input) { ... on PostActionSuccess { post { " + POST_FIELDS + " } } ... on MutationError { message } } }"
    result = _gql(query, envelope["variables"])[method]
    if result.get("message") or not result.get("post", {}).get("id"):
        raise BufferError("Buffer did not confirm a post ID; reconcile the pending operation")
    return result["post"]


class BufferProvider:
    def __init__(self, destination):
        self.destination = destination

    def observation(self):
        d = self.destination
        if not any(o["id"] == d.organization_id for o in get_account()["organizations"]):
            raise BufferError("Buffer organization is not accessible")
        from config import BUFFER_CONFIG, _read_file
        import os
        safety.check_configuration(d, os.environ.get("BUFFER_CHANNEL_ID"), _read_file(BUFFER_CONFIG / "channel_id"))
        channels = [c for c in get_channels(d.organization_id) if c["id"] == d.buffer_id]
        if len(channels) != 1:
            raise BufferError("Expected Buffer channel is not accessible")
        ch = channels[0]
        return {"checked_at": datetime.now(timezone.utc).isoformat(), "name": ch["name"], "service": ch["service"],
                "youtube_id": ch["serviceId"], "buffer_id": ch["id"], "organization_id": ch["organizationId"],
                "can_write": "scheduleUpdates" in ch["allowedActions"], "disconnected": ch["isDisconnected"],
                "locked": ch["isLocked"], "paused": ch["isQueuePaused"]}

    def occupied_slots(self):
        return [p["dueAt"] for p in list_posts(self.destination.organization_id, self.destination.buffer_id)
                if p.get("dueAt") and p.get("status") not in ("sent", "draft", "error")]

    def submit(self, plan, staged, ledger):
        safety.require_live_publishing()
        ledger.require_pending(plan)
        safety.validate_plan(plan, datetime.now(timezone.utc))
        safety.check_observation(self.destination, self.observation(), datetime.now(timezone.utc))
        if plan["operation"] == "buffer_promote":
            current = get_post(self.destination.organization_id, plan["remote_id"])
            safety.buffer_edit_request(plan, current)
        else:
            from storage import upload_video, verify_public_media
            expected = safety.expected_media_url(plan)
            if not expected:
                raise BufferError("An explicit immutable storage destination is required")
            if plan.get("prehosted_url"):
                if plan["prehosted_url"] != expected:
                    raise BufferError("Pre-hosted URL differs from the approved immutable object")
                verify_public_media(expected, plan["sha256"])
            else:
                actual = upload_video(str(staged), plan=plan, ledger=ledger)
                if actual != expected:
                    raise BufferError("Storage returned an unapproved media URL")
        post = _mutate(plan["provider_request"])
        ledger.record_remote_id(plan["plan_id"], post["id"])
        actual = get_post(self.destination.organization_id, post["id"])
        metadata = actual.get("metadata") or {}
        if actual.get("id") != post["id"]:
            raise BufferError("Buffer readback returned a different post")
        if actual.get("channelId") != self.destination.buffer_id or metadata.get("title") != plan["title"] or actual.get("text", "") != plan["description"] or metadata.get("privacy") != plan["visibility"]:
            raise BufferError("Buffer readback differs from the approved channel/metadata")
        if plan["operation"] != "buffer_promote":
            expected = {"madeForKids": plan["made_for_kids"], "notifySubscribers": plan["notify_subscribers"],
                        "category": {"categoryId": plan["category_id"]}}
        else:
            expected = {k: v for k, v in (plan["remote_snapshot"].get("metadata") or {}).items()
                        if k in ("madeForKids", "notifySubscribers", "category")}
        if any(metadata.get(k) != v for k, v in expected.items()):
            raise BufferError("Buffer audience, notification or category differs from the reviewed settings")
        due = actual.get("dueAt")
        if bool(due) != bool(plan["due_at"]) or (due and safety.aware(due) != safety.aware(plan["due_at"])):
            raise BufferError("Buffer schedule differs from the approved time")
        if plan["operation"] == "buffer_draft" and actual.get("status") != "draft":
            raise BufferError("Buffer did not create a saved draft")
        if plan["operation"] != "buffer_draft" and actual.get("status") != "scheduled":
            raise BufferError("Buffer did not confirm the reviewed schedule")
        if plan["remote_id"] and actual["id"] != plan["remote_id"]:
            raise BufferError("Buffer returned a different post")
        return {"id": actual["id"], "destination": plan["destination_key"], "sha256": plan["sha256"],
                "plan_id": plan["plan_id"], "due_at": plan["due_at"], "visibility": plan["visibility"]}
