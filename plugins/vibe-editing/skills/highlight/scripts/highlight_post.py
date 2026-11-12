#!/usr/bin/env python3
"""Review scheduling for explicitly selected, identity-bound YouTube drafts."""
import argparse
import datetime as dt
import os as _os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import distribution_safety as safety
import distribution_runtime as runtime
from distribution_youtube import get_service, list_uploads
from title_sop import build_title1_system, local_title

DEFAULT_SECRET = str(ROOT / "config/youtube_client_secret.json")
DEFAULT_TOKEN = str(ROOT / "config/youtube_token.json")

def make_title(text, use_llm=True):
    """Subs-optimized title. LLM if ANTHROPIC_API_KEY is set, else key-free local titler."""
    key = _os.environ.get("ANTHROPIC_API_KEY")
    if use_llm and key and text.strip():
        try:
            import urllib.request, json as _json
            body = {
                "model": _os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest"),
                "max_tokens": 64,
                "system": build_title1_system(),
                "messages": [{"role": "user", "content": f"TRANSCRIPT:\n{text[:6000]}\n\nWrite the title."}],
            }
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=_json.dumps(body).encode(),
                headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=40) as r:
                j = _json.loads(r.read())
            out = "".join(b.get("text", "") for b in j.get("content", [])).strip().splitlines()
            if out and out[0].strip():
                return out[0].strip().strip('"').strip()[:95] if not out[0].strip().startswith('"') else out[0].strip()[:95]
        except Exception as e:
            print(f"  (LLM titler failed: {e} — using local titler)")
    return local_title(text)[:95]





def list_private_uploads(svc, channel_id, limit):
    # Inventory is never itself authorization to schedule every private video.
    return [v for v in list_uploads(svc, channel_id)
            if v.get("status", {}).get("privacyStatus") == "private" and not v["status"].get("publishAt")][:limit]


def next_slots(n, slots, tz_name, start=None, occupied=()):
    if n == 0:
        return []
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise safety.Blocked("Invalid timezone; no fallback to UTC") from exc
    if n < 0 or not slots or len(set(slots)) != len(slots):
        raise safety.Blocked("Slots must be nonempty and unique")
    hhmm = []
    for value in slots:
        if not re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", value) or int(value[:2]) > 23:
            raise safety.Blocked("Slots must use valid HH:MM")
        hhmm.append(tuple(int(part) for part in value.split(":")))
    now = start or dt.datetime.now(tz)
    if now.tzinfo is None:
        raise safety.Blocked("Start time needs a timezone")
    now = now.astimezone(tz)
    taken = {safety.aware(v) for v in occupied}
    out, day = [], now.date()
    for _ in range(370):
        for h, m in sorted(hhmm):
            cand = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
            # Skip DST gaps; ambiguous repeated hours require an explicit offset.
            if cand.astimezone(dt.timezone.utc).astimezone(tz).replace(tzinfo=None) != cand.replace(tzinfo=None):
                continue
            if cand.replace(fold=1).utcoffset() != cand.utcoffset():
                raise safety.Blocked("Ambiguous DST slot; use an exact timestamp with offset")
            utc = cand.astimezone(dt.timezone.utc)
            if cand > now + dt.timedelta(minutes=5) and utc not in taken:
                out.append(utc.isoformat().replace("+00:00", "Z"))
                if len(out) >= n:
                    return out
        day += dt.timedelta(days=1)
    if n == 0:
        return []
    raise safety.Blocked("Could not find enough free slots within a year")


def main():
    parser = argparse.ArgumentParser(description="Preview exact YouTube scheduling plans; live mutations disabled")
    parser.add_argument("--channel-id")
    parser.add_argument("--selection", type=Path, help="JSON list of explicit {video_path, video_id, publish_at?} selections")
    parser.add_argument("--snapshot", type=Path, help="Offline {channel_id, videos} provider snapshot for review")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--slots", default="12:00,18:00")
    parser.add_argument("--tz")
    parser.add_argument("--client-secret")
    parser.add_argument("--token")
    parser.add_argument("--no-llm", action="store_true", help="Use the existing local titler (the default)")
    parser.add_argument("--use-llm", action="store_true", help="Explicitly opt into the existing optional paid titler")
    parser.add_argument("--no-retitle", action="store_true")
    runtime.add_arguments(parser)
    args = parser.parse_args()
    runtime.validate_cli_mode(args)
    plans = []
    if not args.execute_plan:
        if not args.selection:
            raise safety.Blocked("An explicit --selection is required; private uploads are not automatically selected")
        if args.limit < 1:
            raise safety.Blocked("Limit must be positive")
        if args.use_llm and (args.no_llm or args.dry_run):
            raise safety.Blocked("Paid titling is incompatible with --no-llm and offline --dry-run")
        d = runtime.destination(args.registry, args.destination, args.channel_id, "youtube")
        if args.tz and args.tz != d.timezone:
            raise safety.Blocked("Timezone conflicts with destination registry")
        if args.token and str(Path(args.token).expanduser()) != str(Path(d.youtube_token_path or DEFAULT_TOKEN).expanduser()):
            raise safety.Blocked("Token override conflicts with destination registry")
        if args.client_secret and str(Path(args.client_secret).expanduser()) != str(Path(d.youtube_client_secret_path or DEFAULT_SECRET).expanduser()):
            raise safety.Blocked("Client-secret override conflicts with destination registry")
        chosen = runtime.read_json(args.selection)
        if not isinstance(chosen, list) or not chosen or len(chosen) > args.limit:
            raise safety.Blocked("Selection must be a nonempty list within the requested limit")
        if args.snapshot:
            snapshot = runtime.read_json(args.snapshot)
            if snapshot.get("channel_id") != d.youtube_id:
                raise safety.Blocked("Snapshot belongs to another channel")
            videos = snapshot["videos"]
        elif args.verify:
            videos = list_uploads(get_service(d.youtube_client_secret_path, d.youtube_token_path), d.youtube_id)
        else:
            raise safety.Blocked("Supply an offline --snapshot or explicitly request read-only --verify")
        by_id = {v["id"]: v for v in videos}
        occupied = [v["status"]["publishAt"] for v in videos if v.get("status", {}).get("publishAt")]
        remote_occupied = {safety.aware(value) for value in occupied}
        for entry in chosen:
            if entry.get("publish_at") and safety.aware(entry["publish_at"]) in remote_occupied:
                raise safety.Blocked("Explicit schedule conflicts with an existing snapshot slot")
        # Reserve explicit times in the generator too, so generated slots cannot
        # collide with another item in this same approved selection.
        occupied += [v["publish_at"] for v in chosen if v.get("publish_at")]
        generated = iter(next_slots(sum(not v.get("publish_at") for v in chosen), [v.strip() for v in args.slots.split(",")], d.timezone, occupied=occupied))
        if args.use_llm:
            from vibe_credentials import load_keys
            load_keys()
        for entry in chosen:
            current = by_id.get(entry["video_id"])
            if current is None:
                raise safety.Blocked("A selected video is missing from the channel snapshot")
            intent = runtime.clip_manifest(entry["video_path"])
            if intent.get("youtube_video_id") != entry["video_id"]:
                raise safety.Blocked("Clip manifest must bind the selected YouTube video ID")
            title = current["snippet"]["title"] if args.no_retitle else intent.get("title")
            if title is None:
                title = make_title(current["snippet"].get("description") or current["snippet"]["title"], use_llm=args.use_llm)
            description = intent.get("description", current["snippet"].get("description", ""))
            when = entry.get("publish_at") or next(generated)
            plans.append(safety.prepare(entry["video_path"], d, intent, title, description, operation="youtube_schedule",
                                        visibility="public", due_at=when, remote_id=entry["video_id"], remote_snapshot=current))
    runtime.run_batch(plans, args)


if __name__ == "__main__":
    try:
        main()
    except (safety.Blocked, OSError, ValueError) as exc:
        raise SystemExit(str(exc))
