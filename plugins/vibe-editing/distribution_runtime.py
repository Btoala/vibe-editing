"""Shared CLI planning and execution path for Buffer and YouTube.

Preparation is offline. --verify performs explicit read-only account checks.
Live mutations remain disabled by distribution_safety.require_live_publishing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import distribution_safety as safety

ROOT = Path(__file__).resolve().parent
DEFAULT_REGISTRY = Path.home() / ".config/vibe-editing/distribution.json"
DEFAULT_STATE = Path.home() / ".local/state/vibe-editing/distribution/history.sqlite3"


def read_json(path):
    try:
        return json.loads(Path(path).expanduser().read_text())
    except (OSError, ValueError) as exc:
        raise safety.Blocked(f"Cannot read valid JSON from {path}") from exc


def destinations(path):
    registry = read_json(path)
    if registry.get("schema") != 1 or not isinstance(registry.get("destinations"), list):
        raise safety.Blocked("Destination registry needs schema=1 and a destinations list")
    result, aliases = [], set()
    for record in registry["destinations"]:
        try:
            d = safety.Destination(**record)
            d.validate()
        except (TypeError, ValueError) as exc:
            raise safety.Blocked("Invalid destination registry record") from exc
        if d.alias in aliases:
            raise safety.Blocked("Duplicate destination alias")
        aliases.add(d.alias)
        result.append(d)
    return result


def destination(path, alias=None, channel_id=None, route=None):
    matches = [d for d in destinations(path) if (alias is None or d.alias == alias)
               and (channel_id is None or d.youtube_id == channel_id) and (route is None or d.route == route)]
    if len(matches) != 1:
        raise safety.Blocked("Select exactly one configured destination; channel names are not IDs")
    return matches[0]


def clip_manifest(path):
    manifest = read_json(str(path) + ".publish.json")
    if manifest.get("schema") != 1:
        raise safety.Blocked("Clip manifest schema must be 1")
    return manifest


def legacy_paths():
    base = ROOT / "skills/highlight"
    return [base / "buffer" / name for name in ("posted_log.json", "url_cache.json", "api_calls.json")] + [base / "workspace/bridge_log.jsonl"]


def add_arguments(parser):
    parser.add_argument("--destination", help="Explicit destination alias in the private registry")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--dry-run", action="store_true", help="Offline preview; no state, token or provider writes")
    parser.add_argument("--verify", action="store_true", help="Read-only provider identity/history check (no login flow)")
    parser.add_argument("--save-plan", type=Path, help="Explicitly save this exact review packet privately")
    parser.add_argument("--execute-plan", type=Path, help="Submit the saved packet after release approval; currently disabled")
    parser.add_argument("--approve-plan-id", help="Exact batch digest shown in the saved review packet")


def packet(plans):
    return {"schema": 1, "plans": plans, "batch_id": safety.digest([p["plan_id"] for p in plans])}


def load_packet(path):
    result = read_json(path)
    if result.get("schema") != 1 or not isinstance(result.get("plans"), list) or not result["plans"]:
        raise safety.Blocked("Invalid review packet")
    if result != packet(result["plans"]):
        raise safety.Blocked("Review packet changed")
    return result


def save_packet(path, review):
    path = Path(path).expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".review-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(review, indent=2, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def check_batch(plans):
    if not plans:
        raise safety.Blocked("No eligible manifest-bound clips")
    hashes, assets, times = set(), set(), set()
    now = datetime.now(timezone.utc)
    for p in plans:
        safety.validate_plan(p, now)
        content, asset = (p["destination_key"], p["sha256"]), (p["destination_key"], p["asset_id"])
        if content in hashes or asset in assets:
            raise safety.Blocked("Duplicate content or editorial asset inside this batch")
        hashes.add(content)
        assets.add(asset)
        if p["due_at"]:
            slot = (p["destination_key"], p["due_at"])
            if slot in times:
                raise safety.Blocked("Two clips request the same channel/time")
            times.add(slot)


def provider_for(d):
    if d.route == "youtube":
        from distribution_youtube import YouTubeProvider
        return YouTubeProvider(d)
    buffer_dir = str(ROOT / "skills/highlight/buffer")
    if buffer_dir not in sys.path:
        sys.path.insert(0, buffer_dir)
    from buffer_api import BufferProvider
    return BufferProvider(d)


def history_review(ledger, p):
    try:
        rows = ledger.history(p["destination_key"])
        duplicates = [r for r in rows if (r["sha256"] == p["sha256"] or r["asset_id"] == p["asset_id"])]
        return {**ledger.summary(p["destination_key"]),
                "related_history": [{k: r[k] for k in ("operation", "status", "remote_id")} for r in duplicates]}
    except safety.Blocked as exc:
        return {"blocked": str(exc), "initialization": "Explicit state initialization and legacy review required"}


def run_batch(plans, args, provider_factory=provider_for):
    if args.dry_run and (args.execute_plan or args.save_plan or args.verify):
        raise safety.Blocked("--dry-run cannot be combined with execution, save-plan or connected verification")
    review = load_packet(args.execute_plan) if args.execute_plan else packet(plans)
    plans = review["plans"]
    check_batch(plans)
    configured = destinations(args.registry)
    ledger = safety.Ledger(args.state)
    observations = {}
    for p in plans:
        d = next((d for d in configured if d.alias == p["destination"]["alias"]), None)
        if d is None or asdict(d) != p["destination"]:
            raise safety.Blocked("Destination configuration changed after review")
        if args.verify:
            observed = provider_factory(d).observation()
            safety.check_observation(d, observed, datetime.now(timezone.utc))
            observations[d.alias] = observed
        print(safety.preview(p, observations.get(d.alias)))
        print(json.dumps({"history": history_review(ledger, p), "channel_daily_attempt_cap": d.daily_cap}, indent=2))
    print(json.dumps({"batch_id": review["batch_id"], "global_daily_attempt_cap": global_cap(),
                      "count": len(plans), "live_publishing": "DISABLED"}, indent=2))
    if args.save_plan:
        save_packet(args.save_plan, review)
        print(f"Saved exact review packet: {args.save_plan}")
    if not args.execute_plan:
        print("Preview complete; no media uploaded, queued, scheduled or published.")
        return review
    if args.approve_plan_id != review["batch_id"]:
        raise safety.Blocked("Explicit approval of this exact batch ID is required")
    safety.require_live_publishing()  # No credentials or remote requests before the release gate.
    for p in plans:
        d = safety.Destination(**p["destination"])
        provider = provider_factory(d)
        observed = provider.observation()
        safety.check_observation(d, observed, datetime.now(timezone.utc))
        if p["due_at"]:
            # Read every configured transport for the canonical channel, not just
            # the active route. Lack of access to one route must not be ignored.
            for peer in configured:
                if peer.key == d.key:
                    occupied = provider_factory(peer).occupied_slots()
                    if safety.aware(p["due_at"]) in {safety.aware(t) for t in occupied}:
                        raise safety.Blocked("Approved schedule conflicts with an existing remote slot")
        def submit(approved):
            staged = safety.stage_media(approved, Path(args.state).parent)
            return provider.submit(approved, staged, ledger)
        safety.exercise(p, d, observed, ledger, submit, approve_id=p["plan_id"], dry_run=False,
                        global_cap=global_cap(), emit=lambda _: None)
    return review


def global_cap():
    try:
        value = int(os.environ.get("VIBE_BUFFER_DAILY_CAP", "10"))
    except ValueError as exc:
        raise safety.Blocked("Invalid daily cap") from exc
    if value < 1:
        raise safety.Blocked("Daily cap must be positive")
    return value


def main():
    parser = argparse.ArgumentParser(description="Initialize or inspect local distribution history; no publishing")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init-state")
    init.add_argument("--state", type=Path, default=DEFAULT_STATE)
    history = sub.add_parser("history")
    history.add_argument("--state", type=Path, default=DEFAULT_STATE)
    history.add_argument("--channel-id", required=True)
    adoption = sub.add_parser("adopt", help="Record an explicitly confirmed local clip/remote ID mapping; provider reads only")
    adoption.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    adoption.add_argument("--destination", required=True)
    adoption.add_argument("--state", type=Path, default=DEFAULT_STATE)
    adoption.add_argument("--video", required=True)
    adoption.add_argument("--remote-id", required=True)
    adoption.add_argument("--confirm-sha256", required=True, help="Full SHA-256 affirming that this local clip matches the remote video")
    adoption.add_argument("--evidence", required=True, help="Human review evidence; never credentials")
    adoption.add_argument("--legacy-source")
    adoption.add_argument("--legacy-key")
    args = parser.parse_args()
    ledger = safety.Ledger(args.state)
    if args.command == "init-state":
        ledger.initialize(legacy_paths())
        print("Private state initialized; legacy files preserved. Unresolved legacy records block submissions.")
    elif args.command == "history":
        print(json.dumps(ledger.history("youtube:" + args.channel_id), indent=2))
    else:
        d = destination(args.registry, args.destination)
        manifest = clip_manifest(args.video)
        sha, _ = safety.full_hash(args.video)
        if sha != args.confirm_sha256 or manifest.get("sha256") != sha or manifest.get("destination") != d.key:
            raise safety.Blocked("Explicit content confirmation and clip manifest must match")
        if bool(args.legacy_source) != bool(args.legacy_key):
            raise safety.Blocked("Legacy source and key must be specified together")
        provider = provider_for(d)
        safety.check_observation(d, provider.observation(), datetime.now(timezone.utc))
        if d.route == "youtube":
            from distribution_youtube import read_video
            remote = read_video(provider.svc, args.remote_id, d.youtube_id)
        else:
            from buffer_api import get_post
            remote = get_post(d.organization_id, args.remote_id)
        ledger.adopt(d, sha, manifest.get("asset_id"), remote, args.evidence, args.legacy_source, args.legacy_key)
        print("Recorded the explicitly confirmed mapping locally; no provider data changed.")


if __name__ == "__main__":
    try:
        main()
    except (safety.Blocked, OSError, ValueError) as exc:
        raise SystemExit(str(exc))
