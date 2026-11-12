#!/usr/bin/env python3
"""Preview a private YouTube upload bound to an explicit clip/channel manifest."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import distribution_safety as safety
import distribution_runtime as runtime

DEFAULT_SECRET = str(ROOT / "config/youtube_client_secret.json")
DEFAULT_TOKEN = str(ROOT / "config/youtube_token.json")


def upload_private(*args, **kwargs):
    """The legacy direct helper cannot bypass the common approval/history path."""
    safety.require_live_publishing()
    raise safety.Blocked("Use a saved manifest-bound plan through the publishing entrypoint")


def main():
    parser = argparse.ArgumentParser(description="Private-upload preview; live mutations disabled")
    parser.add_argument("--video")
    parser.add_argument("--title")
    parser.add_argument("--description")
    parser.add_argument("--channel-id", help="Expected YouTube channel ID; must agree with registry")
    parser.add_argument("--client-secret")
    parser.add_argument("--token")
    parser.add_argument("--yes-upload", action="store_true", help="Legacy consent flag; a saved approved plan is also required")
    runtime.add_arguments(parser)
    args = parser.parse_args()
    runtime.validate_cli_mode(args)
    plans = []
    if not args.execute_plan:
        if not args.video:
            parser.error("--video is required when preparing a plan")
        d = runtime.destination(args.registry, args.destination, args.channel_id, "youtube")
        if args.token and str(Path(args.token).expanduser()) != str(Path(d.youtube_token_path or DEFAULT_TOKEN).expanduser()):
            raise safety.Blocked("Token override conflicts with destination registry")
        if args.client_secret and str(Path(args.client_secret).expanduser()) != str(Path(d.youtube_client_secret_path or DEFAULT_SECRET).expanduser()):
            raise safety.Blocked("Client-secret override conflicts with destination registry")
        intent = runtime.clip_manifest(args.video)
        title = args.title if args.title is not None else intent.get("title", Path(args.video).name)
        description = args.description if args.description is not None else intent.get("description", "")
        plans = [safety.prepare(args.video, d, intent, title, description, made_for_kids=intent.get("made_for_kids", False),
                                notify_subscribers=intent.get("notify_subscribers", False))]
    runtime.run_batch(plans, args)
    if args.yes_upload and not args.execute_plan:
        raise safety.Blocked("--yes-upload cannot bypass exact-plan approval; live publishing remains disabled")


if __name__ == "__main__":
    try:
        main()
    except (safety.Blocked, OSError, ValueError) as exc:
        raise SystemExit(str(exc))
