#!/usr/bin/env python3
"""Prepare manifest-bound Buffer plans. Default and --dry-run never publish."""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import distribution_safety as safety
import distribution_runtime as runtime
from captions import make_caption, make_title
from config import shorts_dir, BUFFER_CONFIG, _read_file

VIDEO_EXTS = (".mp4", ".mov", ".webm", ".m4v")


def clip_key(video_path):
    return safety.full_hash(video_path)[0]


def build_plans(folder, total, order, destination, video_url=None, due_at=None, visibility=None):
    if total < 1:
        raise safety.Blocked("Total must be positive")
    if video_url and total != 1:
        raise safety.Blocked("--video-url requires --total 1")
    if due_at and total != 1:
        raise safety.Blocked("A shared --due-at requires --total 1; batches use each clip's manifest schedule")
    if not folder.is_dir():
        raise safety.Blocked("Shorts folder does not exist")
    safety.check_configuration(destination, os.environ.get("BUFFER_CHANNEL_ID"), _read_file(BUFFER_CONFIG / "channel_id"))
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    files.sort(key=(lambda p: p.name) if order == "name" else (lambda p: p.stat().st_mtime), reverse=order != "name")
    plans = []
    for path in files[:total]:
        intent = runtime.clip_manifest(path)
        title = intent.get("title", make_title(str(path)))
        description = intent.get("description", make_caption(str(path), title))
        when = due_at or intent.get("publish_at")
        privacy = visibility or intent.get("visibility", "private")
        remote_id = intent.get("buffer_post_id")
        operation = "buffer_promote" if remote_id else ("buffer_schedule" if when else "buffer_draft")
        plan = safety.prepare(path, destination, intent, title, description, operation=operation,
                              visibility=privacy, due_at=when, remote_id=remote_id,
                              remote_snapshot=intent.get("buffer_snapshot"),
                              made_for_kids=intent.get("made_for_kids", False), notify_subscribers=intent.get("notify_subscribers", False))
        if video_url:
            if video_url != safety.expected_media_url(plan):
                raise safety.Blocked("Pre-hosted URL must be the configured immutable object for this full hash")
            plan["prehosted_url"] = video_url
            plan["plan_id"] = safety.digest({k: v for k, v in plan.items() if k != "plan_id"})
        plans.append(plan)
    return plans


def main():
    parser = argparse.ArgumentParser(description="Preview and verify Buffer distribution; live publishing disabled")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("discover", help="Read-only list of Buffer and external channel IDs")
    slots = sub.add_parser("slots", help="Uncommitted slot estimates only")
    slots.add_argument("--count", "-n", type=int, default=24)
    fill = sub.add_parser("fill", help="Build a complete preview (the default behavior)")
    fill.add_argument("--dir")
    fill.add_argument("--total", type=int, default=runtime.global_cap())
    fill.add_argument("--order", choices=["mtime", "name"], default="mtime")
    fill.add_argument("--video-url")
    fill.add_argument("--due-at", help="Exact timestamp with UTC offset, for a single clip")
    fill.add_argument("--visibility", choices=["private", "unlisted", "public"])
    runtime.add_arguments(fill)
    args = parser.parse_args()
    if args.command == "discover":
        from buffer_api import discover
        discover()
    elif args.command == "slots":
        from slots import preview_slots
        preview_slots(args.count)
    elif args.command == "fill":
        if args.execute_plan:
            plans = []
        else:
            d = runtime.destination(args.registry, args.destination, route="buffer")
            plans = build_plans(shorts_dir(args.dir), args.total, args.order, d, args.video_url, args.due_at, args.visibility)
        runtime.run_batch(plans, args)
    else:
        parser.print_help()


if __name__ == "__main__":
    try:
        main()
    except (safety.Blocked, OSError, ValueError) as exc:
        raise SystemExit(str(exc))
