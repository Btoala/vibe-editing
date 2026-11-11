"""Exercise real publishing entrypoints and adapters using synthetic media/mocks."""
import contextlib
import copy
import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
BUFFER = ROOT / "skills/highlight/buffer"
SCRIPTS = ROOT / "skills/highlight/scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BUFFER))
import distribution_safety as safety
import distribution_runtime as runtime
import distribution_youtube as youtube
import buffer_api
import config as buffer_config
import storage


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


fill = module("distribution_fill", BUFFER / "fill_queue.py")
bridge = module("distribution_bridge", SCRIPTS / "highlight_publish_bridge.py")
post = module("distribution_post", SCRIPTS / "highlight_post.py")
A, B = "UC" + "A" * 22, "UC" + "B" * 22


def receipt(p):
    return {"id": p["remote_id"] or "synthetic-remote", "destination": p["destination_key"],
            "sha256": p["sha256"], "plan_id": p["plan_id"], "due_at": p["due_at"], "visibility": p["visibility"]}


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise unittest.SkipTest("Existing FFmpeg required for real entrypoint tests")
        cls.media_temp = tempfile.TemporaryDirectory()
        cls.media = Path(cls.media_temp.name)
        for name, size in (("short.mp4", "90x160"), ("mid.mp4", "160x90")):
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=blue:s={size}:r=2:d=10",
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000:duration=10",
                            "-c:v", "mpeg4", "-q:v", "15", "-c:a", "aac", "-y", str(cls.media/name)], check=True, timeout=60)

    @classmethod
    def tearDownClass(cls):
        cls.media_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clips = self.root / "clips"
        self.clips.mkdir()
        self.short, self.mid = self.clips / "short.mp4", self.root / "mid.mp4"
        shutil.copyfile(self.media / "short.mp4", self.short)
        shutil.copyfile(self.media / "mid.mp4", self.mid)
        self.d = safety.Destination("spawn-buffer", "Synthetic Spawn Point", A, "buffer", buffer_id="buffer-A", organization_id="org-A",
                                    storage_base_url="https://synthetic.invalid", storage_bucket="videos")
        self.y = replace(self.d, alias="spawn-youtube", route="youtube")
        self.registry, self.state = self.root / "registry.json", self.root / "private-state/history.sqlite3"
        self.registry.write_text(json.dumps({"schema": 1, "destinations": [asdict(self.d), asdict(self.y)]}))
        self.intent(self.short, self.d)
        self.intent(self.mid, self.y, youtube_video_id="synthetic-remote")
        self.now = datetime.now(timezone.utc)
        self.due = (self.now + timedelta(days=1)).replace(microsecond=0).isoformat()
        self.patches = [patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")),
                        patch.object(socket, "create_connection", side_effect=AssertionError("Network forbidden")),
                        patch.object(buffer_config, "load_keys"), patch.object(buffer_config, "BUFFER_CONFIG", self.root / "buffer-config"),
                        patch.object(fill, "BUFFER_CONFIG", self.root / "buffer-config"),
                        patch.dict(os.environ, {"BUFFER_CHANNEL_ID": self.d.buffer_id})]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def intent(self, path, d, **extra):
        value = {"schema": 1, "destination": d.key, "sha256": safety.full_hash(path)[0], "asset_id": path.name,
                 "title": "Synthetic title", "description": "Complete description\nSecond line", **extra}
        Path(str(path) + ".publish.json").write_text(json.dumps(value))
        return value

    def args(self, d=None, **kwargs):
        return types.SimpleNamespace(registry=self.registry, state=self.state, dry_run=False, verify=False,
                                     save_plan=None, execute_plan=None, approve_plan_id=None, **kwargs)

    def buffer_plan(self):
        return fill.build_plans(self.clips, 1, "name", self.d)[0]

    def youtube_plan(self):
        return safety.prepare(self.mid, self.y, runtime.clip_manifest(self.mid), "Synthetic title", "Complete description\nSecond line")

    def call(self, mod, args):
        output = io.StringIO()
        with patch.object(sys, "argv", [str(mod.__file__), *args]), contextlib.redirect_stdout(output):
            mod.main()
        return output.getvalue()

    def common(self, d):
        return ["--registry", str(self.registry), "--destination", d.alias, "--state", str(self.state)]

    def test_actual_buffer_dry_run_is_offline_and_writes_nothing(self):
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        output = self.call(fill, ["fill", "--dir", str(self.clips), "--total", "1", "--dry-run", *self.common(self.d)])
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertIn('"youtube_channel_id": "' + A, output)
        self.assertIn('"description": "Complete description\\nSecond line"', output)
        self.assertIn('"privacy": "private"', output)
        self.assertIn("UNVERIFIED", output)
        buffer_config.load_keys.assert_not_called()

    def test_buffer_default_invocation_is_also_preview(self):
        output = self.call(fill, ["fill", "--dir", str(self.clips), "--total", "1", *self.common(self.d)])
        self.assertIn("no media uploaded", output)
        self.assertFalse(self.state.exists())

    def test_actual_upload_bridge_default_is_private_preview(self):
        output = self.call(bridge, ["--video", str(self.mid), *self.common(self.y)])
        self.assertIn('"method": "videos.insert"', output)
        self.assertIn('"privacyStatus": "private"', output)
        self.assertIn('"sha256"', output)
        self.assertFalse(self.state.exists())

    def test_legacy_upload_flag_cannot_bypass_plan(self):
        with self.assertRaisesRegex(safety.Blocked, "cannot bypass"):
            self.call(bridge, ["--video", str(self.mid), "--yes-upload", *self.common(self.y)])
        self.assertFalse(self.state.exists())

    def test_wrong_environment_destination_blocks_actual_buffer_entrypoint(self):
        with patch.dict(os.environ, {"BUFFER_CHANNEL_ID": "wrong"}), self.assertRaisesRegex(safety.Blocked, "conflicts"):
            self.call(fill, ["fill", "--dir", str(self.clips), "--total", "1", *self.common(self.d)])

    def test_file_configuration_conflict_is_rejected(self):
        (self.root / "buffer-config").mkdir()
        (self.root / "buffer-config/channel_id").write_text("wrong")
        with self.assertRaisesRegex(safety.Blocked, "conflicts"):
            self.buffer_plan()

    def test_missing_clip_manifest_is_blocked(self):
        Path(str(self.short) + ".publish.json").unlink()
        with self.assertRaises(safety.Blocked):
            self.buffer_plan()

    def test_prehosted_url_cannot_cover_multiple_local_clips(self):
        with self.assertRaisesRegex(safety.Blocked, "total 1"):
            fill.build_plans(self.clips, 2, "name", self.d, video_url="https://media.invalid/a")

    def test_unbound_prehosted_url_is_rejected(self):
        with self.assertRaisesRegex(safety.Blocked, "immutable"):
            fill.build_plans(self.clips, 1, "name", self.d, video_url="https://media.invalid/a")

    def test_batch_contains_no_duplicate_content_even_under_new_names(self):
        second = self.clips / "renamed.mp4"
        shutil.copyfile(self.short, second)
        self.intent(second, self.d)
        with self.assertRaisesRegex(safety.Blocked, "Duplicate"):
            runtime.check_batch(fill.build_plans(self.clips, 2, "name", self.d))

    def test_save_and_reload_binds_exact_provider_payload(self):
        p = self.buffer_plan()
        review = runtime.packet([p])
        saved = self.root / "review.distribution-plan.json"
        runtime.save_packet(saved, review)
        self.assertEqual(runtime.load_packet(saved), review)
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        changed = copy.deepcopy(review)
        changed["plans"][0]["provider_request"]["variables"]["input"]["metadata"]["youtube"]["privacy"] = "public"
        with self.assertRaises(safety.Blocked):
            runtime.check_batch(changed["plans"])

    def test_matching_approval_still_cannot_enable_live_publishing(self):
        review = runtime.packet([self.buffer_plan()])
        saved = self.root / "review.json"
        runtime.save_packet(saved, review)
        args = self.args()
        args.execute_plan, args.approve_plan_id = saved, review["batch_id"]
        factory = Mock()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(safety.Blocked, "disabled"):
            runtime.run_batch([], args, provider_factory=factory)
        factory.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_dry_run_rejects_write_or_connected_flags(self):
        for key, value in (("save_plan", self.root/"out.json"), ("verify", True), ("execute_plan", self.root/"in.json")):
            args = self.args()
            args.dry_run = True
            setattr(args, key, value)
            with self.subTest(key=key), self.assertRaises(safety.Blocked):
                runtime.run_batch([self.buffer_plan()], args)

    def test_all_legacy_mutation_helpers_are_closed_before_credentials(self):
        calls = [lambda: buffer_api.create_video_post("url", "description", "title"),
                 lambda: buffer_api._gql("mutation { createPost(input: {}) { id } }"),
                 lambda: storage.upload_video(str(self.short)),
                 lambda: bridge.upload_private(str(self.mid), "title", "secret", "token")]
        for call in calls:
            with self.subTest(call=call), self.assertRaisesRegex(safety.Blocked, "disabled"):
                call()
        buffer_config.load_keys.assert_not_called()

    def snapshot(self):
        return {"id": "synthetic-remote", "etag": "etag-1", "snippet": {"channelId": A, "title": "Synthetic title",
                "description": "Complete description\nSecond line", "categoryId": "20", "tags": ["keep-tag"]},
                "status": {"privacyStatus": "private", "license": "youtube", "embeddable": False}}

    def test_actual_scheduler_previews_selected_ids_and_preserves_metadata(self):
        selection, snapshot = self.root/"selection.json", self.root/"snapshot.json"
        selection.write_text(json.dumps([{"video_path": str(self.mid), "video_id": "synthetic-remote", "publish_at": self.due}]))
        snapshot.write_text(json.dumps({"channel_id": A, "videos": [self.snapshot()]}))
        output = self.call(post, ["--selection", str(selection), "--snapshot", str(snapshot), "--no-retitle", "--dry-run", *self.common(self.y)])
        self.assertIn('"part": "status"', output)
        self.assertIn('"visibility_at_scheduled_delivery": "public"', output)
        self.assertIn('"description": "Complete description\\nSecond line"', output)
        self.assertFalse(self.state.exists())

    def test_scheduler_never_selects_all_private_videos_implicitly(self):
        with self.assertRaisesRegex(safety.Blocked, "selection"):
            self.call(post, ["--channel-id", A, "--dry-run", *self.common(self.y)])

    def test_scheduler_rejects_wrong_snapshot_channel(self):
        sel, snap = self.root/"selection.json", self.root/"snapshot.json"
        sel.write_text(json.dumps([{"video_path": str(self.mid), "video_id": "synthetic-remote"}]))
        snap.write_text(json.dumps({"channel_id": B, "videos": [self.snapshot()]}))
        with self.assertRaisesRegex(safety.Blocked, "another channel"):
            self.call(post, ["--selection", str(sel), "--snapshot", str(snap), *self.common(self.y)])

    def test_schedule_slot_validation_covers_duplicates_timezone_and_occupied(self):
        start = datetime(2030, 1, 1, 10, tzinfo=timezone.utc)
        with self.assertRaises(safety.Blocked):
            post.next_slots(2, ["12:00", "12:00"], "UTC", start)
        with self.assertRaises(safety.Blocked):
            post.next_slots(1, ["12:00"], "Invalid/Zone", start)
        self.assertEqual(post.next_slots(0, ["12:00"], "UTC", start), [])
        result = post.next_slots(1, ["12:00", "18:00"], "UTC", start, ["2030-01-01T12:00:00Z"])
        self.assertEqual(result, ["2030-01-01T18:00:00Z"])

    def test_immutable_staging_isolated_from_rerender(self):
        p = self.buffer_plan()
        staged = safety.stage_media(p, self.root/"staging")
        original = staged.read_bytes()
        self.short.write_bytes(b"replacement render")
        self.assertEqual(staged.read_bytes(), original)
        self.assertEqual(staged.stat().st_mode & 0o777, 0o400)

    def test_legacy_initialization_backs_up_and_blocks_unknown_history(self):
        legacy = self.root/"posted_log.json"
        legacy.write_text('{"clip":["2026-11-10"]}')
        ledger = safety.Ledger(self.state)
        ledger.initialize([legacy])
        self.assertEqual(ledger.summary(self.d.key)["unresolved_legacy_records"], 1)
        self.assertEqual(next((self.state.parent/"legacy-backup").iterdir()).read_bytes(), legacy.read_bytes())
        with self.assertRaisesRegex(safety.Blocked, "legacy"):
            ledger.reserve(self.buffer_plan(), self.now)

    def test_changed_legacy_file_is_detected_even_if_initially_absent(self):
        legacy = self.root/"posted_log.json"
        ledger = safety.Ledger(self.state)
        ledger.initialize([legacy])
        legacy.write_text('{"new":["today"]}')
        with self.assertRaisesRegex(safety.Blocked, "changed"):
            ledger.reserve(self.buffer_plan(), self.now)

    def test_existing_daily_usage_cannot_be_reset_by_migration(self):
        legacy = self.root/"api_calls.json"
        legacy.write_text(json.dumps({self.now.date().isoformat(): 10}))
        ledger = safety.Ledger(self.state)
        ledger.initialize([legacy])
        with self.assertRaisesRegex(safety.Blocked, "Global"):
            ledger.reserve(self.buffer_plan(), self.now)

    def test_pending_session_is_private_and_never_printed_in_history(self):
        p = self.youtube_plan()
        ledger = safety.Ledger(self.state)
        ledger.initialize()
        ledger.reserve(p, self.now)
        uri = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=SYNTHETIC_SECRET"
        ledger.save_session(p["plan_id"], uri)
        self.assertEqual(ledger.session(p["plan_id"]), uri)
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(ledger.history(self.y.key)))
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

    def test_remote_id_survives_later_unknown_result(self):
        p = self.buffer_plan()
        ledger = safety.Ledger(self.state)
        ledger.initialize()
        ledger.reserve(p, self.now)
        ledger.record_remote_id(p["plan_id"], "fake-post")
        ledger.finish(p["plan_id"], "unknown")
        self.assertEqual(ledger.history(self.d.key)[0]["remote_id"], "fake-post")

    def test_explicit_adoption_binds_manual_upload_without_remote_write(self):
        p = self.youtube_plan()
        ledger = safety.Ledger(self.state)
        ledger.initialize()
        ledger.adopt(self.y, p["sha256"], p["asset_id"], self.snapshot(), "Human compared source and remote draft")
        scheduled = safety.prepare(self.mid, self.y, runtime.clip_manifest(self.mid), p["title"], p["description"], operation="youtube_schedule",
                                   visibility="public", due_at=self.due, remote_id="synthetic-remote", remote_snapshot=self.snapshot())
        ledger.reserve(scheduled, self.now)
        with self.assertRaises(safety.Blocked):
            ledger.reserve(p, self.now)

    def test_buffer_draft_promotion_updates_same_post_instead_of_recreating(self):
        p = self.buffer_plan()
        ledger = safety.Ledger(self.state)
        ledger.initialize()
        ledger.reserve(p, self.now)
        ledger.finish(p["plan_id"], "confirmed", "same-post")
        current = {"id": "same-post", "channelId": self.d.buffer_id, "status": "draft",
                   "assets": [{"id": "synthetic-asset", "mimeType": "video/mp4", "source": safety.expected_media_url(p)}]}
        promoted = safety.prepare(self.short, self.d, runtime.clip_manifest(self.short), p["title"], p["description"], operation="buffer_promote",
                                  visibility="public", due_at=self.due, remote_id="same-post", remote_snapshot=current)
        self.assertEqual(promoted["provider_request"]["method"], "editPost")
        self.assertEqual(promoted["provider_request"]["variables"]["input"]["id"], "same-post")
        self.assertNotIn("assets", promoted["provider_request"]["variables"]["input"])
        ledger.reserve(promoted, self.now)
        with self.assertRaises(safety.Blocked):
            ledger.reserve(promoted, self.now)

    def test_real_runtime_dispatches_only_to_fake_provider_after_test_gate_patch(self):
        p = self.buffer_plan()
        review, saved = runtime.packet([p]), self.root/"review.json"
        runtime.save_packet(saved, review)
        ledger = safety.Ledger(self.state)
        ledger.initialize()
        args = self.args()
        args.execute_plan, args.approve_plan_id = saved, review["batch_id"]
        provider = Mock()
        provider.observation.return_value = {"checked_at": datetime.now(timezone.utc).isoformat(), "name": self.d.name, "service": "youtube",
            "youtube_id": A, "buffer_id": self.d.buffer_id, "organization_id": self.d.organization_id, "can_write": True,
            "disconnected": False, "locked": False, "paused": False}
        provider.submit.side_effect = lambda plan, staged, state: receipt(plan)
        with patch.object(safety, "require_live_publishing"), contextlib.redirect_stdout(io.StringIO()):
            runtime.run_batch([], args, provider_factory=lambda d: provider)
            with self.assertRaises(safety.Blocked):
                runtime.run_batch([], args, provider_factory=lambda d: provider)
        self.assertEqual(provider.submit.call_count, 1)
        self.assertEqual(ledger.history(self.d.key)[0]["status"], "confirmed")

    def test_interrupted_migration_never_exposes_partial_history(self):
        legacy = self.root / "posted_log.json"
        legacy.write_text('{"clip": "old-post"}')
        ledger = safety.Ledger(self.state)
        with patch.object(Path, "write_bytes", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                ledger.initialize([legacy])
        self.assertFalse(self.state.exists())
        with self.assertRaises(safety.Blocked):
            ledger.history(self.d.key)
        ledger.initialize([legacy])
        self.assertEqual(ledger.summary(self.d.key)["unresolved_legacy_records"], 1)

    def test_reinitialization_never_replaces_existing_history(self):
        ledger = safety.Ledger(self.state)
        ledger.initialize()
        ledger.reserve(self.buffer_plan(), self.now)
        before = self.state.read_bytes()
        with self.assertRaises(FileExistsError):
            ledger.initialize()
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(len(ledger.history(self.d.key)), 1)

    def test_low_level_youtube_upload_is_gated_before_sdk_or_credentials(self):
        provider = youtube.YouTubeProvider(self.y)
        with self.assertRaisesRegex(safety.Blocked, "disabled"):
            provider._upload({}, self.mid, Mock())
        self.assertIsNone(provider._service)

    def upload_fixture(self):
        p = self.youtube_plan()
        ledger = safety.Ledger(self.state)
        ledger.initialize()
        ledger.reserve(p, self.now)
        staged = safety.stage_media(p, self.state.parent)
        uri = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=SYNTHETIC"
        original = Mock(return_value=({"location": uri}, b""))
        request = Mock()
        request.http = types.SimpleNamespace(request=original)
        service = MagicMock()
        service.videos.return_value.insert.return_value = request
        sdk = types.ModuleType("googleapiclient.http")
        sdk.MediaFileUpload = Mock()
        return p, ledger, staged, uri, original, request, service, sdk

    def test_resumable_session_is_durable_before_first_chunk(self):
        p, ledger, staged, uri, original, request, service, sdk = self.upload_fixture()
        events = []
        def chunk(**kwargs):
            request.http.request("https://www.googleapis.com/upload/youtube/v3/videos", method="POST")
            self.assertEqual(ledger.session(p["plan_id"]), uri)
            events.append("chunk sent after checkpoint")
            return None, {"id": "synthetic-remote"}
        request.next_chunk.side_effect = chunk
        with patch.dict(sys.modules, {"googleapiclient.http": sdk}), patch.object(safety, "require_live_publishing"):
            response = youtube.YouTubeProvider(self.y, service)._upload(p, staged, ledger)
        self.assertEqual(response["id"], "synthetic-remote")
        self.assertEqual(events, ["chunk sent after checkpoint"])
        self.assertIs(request.http.request, original)
        self.assertEqual(sdk.MediaFileUpload.call_args.kwargs["mimetype"], "video/mp4")
        request.next_chunk.assert_called_once_with(num_retries=0)

    def test_failed_session_checkpoint_prevents_first_chunk(self):
        p, ledger, staged, uri, original, request, service, sdk = self.upload_fixture()
        chunk_sent = Mock()
        def chunk(**kwargs):
            request.http.request("https://www.googleapis.com/upload/youtube/v3/videos", method="POST")
            chunk_sent()
            return None, {"id": "synthetic-remote"}
        request.next_chunk.side_effect = chunk
        with patch.dict(sys.modules, {"googleapiclient.http": sdk}), patch.object(safety, "require_live_publishing"), \
                patch.object(ledger, "save_session", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                youtube.YouTubeProvider(self.y, service)._upload(p, staged, ledger)
        chunk_sent.assert_not_called()
        self.assertIs(request.http.request, original)

    def test_storage_conflict_uses_full_hash_and_verifies_bytes_without_overwrite(self):
        p = self.buffer_plan()
        ledger = safety.Ledger(self.state)
        ledger.initialize()
        ledger.reserve(p, self.now)
        fake = Mock()
        missing, found = MagicMock(), MagicMock()
        missing.status_code, found.status_code = 404, 200
        missing.__enter__.return_value, found.__enter__.return_value = missing, found
        found.iter_content.return_value = [self.short.read_bytes()]
        fake.get.side_effect = [missing, found]
        uploaded = []
        def post(url, **kwargs):
            uploaded.append(kwargs["data"].read())
            self.assertEqual(kwargs["headers"]["x-upsert"], "false")
            self.assertEqual(kwargs["headers"]["Content-Type"], "video/mp4")
            self.assertFalse(kwargs["allow_redirects"])
            self.assertTrue(url.endswith("/" + p["sha256"]))
            return types.SimpleNamespace(status_code=409)
        fake.post.side_effect = post
        with patch.dict(sys.modules, {"requests": fake}), patch.object(safety, "require_live_publishing"), \
                patch.object(storage, "storage_url", return_value=self.d.storage_base_url), \
                patch.object(storage, "storage_bucket", return_value=self.d.storage_bucket), \
                patch.object(storage, "storage_key", return_value="SYNTHETIC_KEY"), \
                patch.object(storage, "STORAGE_PREFIX", self.d.storage_prefix):
            url = storage.upload_video(self.short, plan=p, ledger=ledger)
        self.assertEqual(uploaded, [self.short.read_bytes()])
        self.assertEqual(url, safety.expected_media_url(p))
        self.assertEqual(fake.get.call_count, 2)

    def test_storage_existing_wrong_bytes_block_before_upload(self):
        p = self.buffer_plan()
        ledger = safety.Ledger(self.state)
        ledger.initialize()
        ledger.reserve(p, self.now)
        fake, response = Mock(), MagicMock()
        response.__enter__.return_value = response
        response.status_code = 200
        response.iter_content.return_value = [b"wrong content"]
        fake.get.return_value = response
        with patch.dict(sys.modules, {"requests": fake}), patch.object(safety, "require_live_publishing"), \
                patch.object(storage, "storage_url", return_value=self.d.storage_base_url), \
                patch.object(storage, "storage_bucket", return_value=self.d.storage_bucket), \
                patch.object(storage, "STORAGE_PREFIX", self.d.storage_prefix), \
                self.assertRaisesRegex(safety.Blocked, "hash"):
            storage.upload_video(self.short, plan=p, ledger=ledger)
        fake.post.assert_not_called()

    def test_youtube_update_sends_reviewed_fields_and_checks_readback(self):
        current = self.snapshot()
        p = safety.prepare(self.mid, self.y, runtime.clip_manifest(self.mid), "Approved new title", current["snippet"]["description"],
                           operation="youtube_schedule", visibility="public", due_at=self.due,
                           remote_id=current["id"], remote_snapshot=current)
        ledger = safety.Ledger(self.state)
        ledger.initialize()
        ledger.adopt(self.y, p["sha256"], p["asset_id"], current, "Synthetic content comparison")
        ledger.reserve(p, self.now)
        service = MagicMock()
        service._vibe_scopes = ("https://www.googleapis.com/auth/youtube.force-ssl",)
        service.channels.return_value.list.return_value.execute.return_value = {"items": [{"id": A, "snippet": {"title": "Synthetic"}}]}
        after = copy.deepcopy(current)
        after["snippet"].update(p["provider_request"]["body"]["snippet"])
        after["status"].update(p["provider_request"]["body"]["status"])
        service.videos.return_value.list.return_value.execute.side_effect = [{"items": [current]}, {"items": [after]}]
        request = service.videos.return_value.update.return_value
        request.headers = {}
        request.execute.return_value = {"id": current["id"]}
        with patch.object(safety, "require_live_publishing"):
            result = youtube.YouTubeProvider(self.y, service).submit(p, self.mid, ledger)
        self.assertEqual(result["id"], current["id"])
        sent = service.videos.return_value.update.call_args.kwargs
        self.assertEqual(sent["body"]["snippet"]["tags"], ["keep-tag"])
        self.assertFalse(sent["body"]["status"]["embeddable"])
        self.assertEqual(request.headers["If-Match"], "etag-1")
        self.assertEqual(ledger.history(self.y.key)[-1]["remote_id"], current["id"])

    def test_buffer_adapter_confirms_exact_post_and_creation_settings(self):
        p = self.buffer_plan()
        ledger = safety.Ledger(self.state)
        ledger.initialize()
        ledger.reserve(p, self.now)
        provider = buffer_api.BufferProvider(self.d)
        observation = {"checked_at": datetime.now(timezone.utc).isoformat(), "name": "Synthetic", "service": "youtube",
            "youtube_id": A, "buffer_id": self.d.buffer_id, "organization_id": self.d.organization_id,
            "can_write": True, "disconnected": False, "locked": False, "paused": False}
        remote = {"id": "synthetic-post", "channelId": self.d.buffer_id, "status": "draft", "dueAt": None,
                  "text": p["description"], "metadata": {"title": p["title"], "privacy": "private", "madeForKids": False,
                  "notifySubscribers": False, "category": {"categoryId": "22"}}}
        remote["assets"] = [{"id": "synthetic-asset", "mimeType": "video/mp4", "source": safety.expected_media_url(p)}]
        with patch.object(safety, "require_live_publishing"), patch.object(provider, "observation", return_value=observation), \
                patch.object(storage, "upload_video", return_value=safety.expected_media_url(p)), \
                patch.object(buffer_api, "_mutate", return_value={"id": remote["id"]}), \
                patch.object(buffer_api, "get_post", return_value=remote):
            self.assertEqual(provider.submit(p, self.short, ledger)["id"], remote["id"])
            remote["metadata"]["notifySubscribers"] = True
            with self.assertRaisesRegex(safety.Blocked, "notification"):
                provider.submit(p, self.short, ledger)
            remote["metadata"]["notifySubscribers"] = False
            remote["id"] = "wrong-post"
            with self.assertRaisesRegex(safety.Blocked, "different post"):
                provider.submit(p, self.short, ledger)

    def test_buffer_schedule_cannot_be_confirmed_as_unscheduled_draft(self):
        p = safety.prepare(self.short, self.d, runtime.clip_manifest(self.short), "Synthetic title", "Description",
                           operation="buffer_schedule", visibility="public", due_at=self.due)
        ledger = safety.Ledger(self.state)
        ledger.initialize()
        ledger.reserve(p, self.now)
        provider = buffer_api.BufferProvider(self.d)
        observation = {"checked_at": datetime.now(timezone.utc).isoformat(), "name": "Synthetic", "service": "youtube",
            "youtube_id": A, "buffer_id": self.d.buffer_id, "organization_id": self.d.organization_id,
            "can_write": True, "disconnected": False, "locked": False, "paused": False}
        remote = {"id": "synthetic-post", "channelId": self.d.buffer_id, "status": "draft", "dueAt": p["due_at"],
                  "text": p["description"], "metadata": {"title": p["title"], "privacy": "public", "madeForKids": False,
                  "notifySubscribers": False, "category": {"categoryId": "22"}}}
        remote["assets"] = [{"id": "synthetic-asset", "mimeType": "video/mp4", "source": safety.expected_media_url(p)}]
        with patch.object(safety, "require_live_publishing"), patch.object(provider, "observation", return_value=observation), \
                patch.object(storage, "upload_video", return_value=safety.expected_media_url(p)), \
                patch.object(buffer_api, "_mutate", return_value={"id": remote["id"]}), \
                patch.object(buffer_api, "get_post", return_value=remote):
            with self.assertRaisesRegex(safety.Blocked, "confirm the reviewed schedule"):
                provider.submit(p, self.short, ledger)


class AdapterContractTests(unittest.TestCase):
    def test_buffer_post_lookup_uses_documented_input_fields(self):
        with patch.object(buffer_api, "_gql", return_value={"post": {"id": "id"}}) as call:
            buffer_api.get_post("org", "id")
        self.assertEqual(call.call_args.args[1], {"input": {"id": "id"}})

    def test_buffer_uses_graphql_variables_and_bounded_timeout(self):
        fake = Mock()
        fake.post.return_value.status_code = 200
        fake.post.return_value.json.return_value = {"data": {"account": {"id": "fake", "organizations": []}}}
        with patch.dict(sys.modules, {"requests": fake}), patch.object(buffer_api, "buffer_token", return_value="FAKE"), patch.object(buffer_api, "BUFFER_API_ENDPOINT", "https://api.buffer.com"):
            buffer_api.get_account()
        self.assertEqual(fake.post.call_args.kwargs["timeout"], (10, 60))
        self.assertEqual(fake.post.call_args.kwargs["json"]["variables"], {})

    def test_youtube_ownership_uses_mine_not_arbitrary_id(self):
        svc = MagicMock()
        svc.channels.return_value.list.return_value.execute.return_value = {"items": [{"id": A, "snippet": {"title": "Synthetic"}}]}
        youtube.owned_channel(svc, A)
        self.assertTrue(svc.channels.return_value.list.call_args.kwargs["mine"])
        self.assertNotIn("id", svc.channels.return_value.list.call_args.kwargs)
        with self.assertRaises(safety.Blocked):
            youtube.owned_channel(svc, B)

    def test_unknown_oauth_scopes_do_not_count_as_write_verification(self):
        svc = MagicMock()
        svc.channels.return_value.list.return_value.execute.return_value = {"items": [{"id": A, "snippet": {"title": "Synthetic"}}]}
        svc._vibe_scopes = ()
        d = safety.Destination("a", "Synthetic", A, "youtube")
        self.assertFalse(youtube.YouTubeProvider(d, svc).observation()["can_write"])
        svc._vibe_scopes = ("https://www.googleapis.com/auth/youtube.force-ssl",)
        self.assertTrue(youtube.YouTubeProvider(d, svc).observation()["can_write"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
