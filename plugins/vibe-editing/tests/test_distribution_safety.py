"""Synthetic regression suite. No credentials and no network permitted."""
import copy
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

# Works both in this standalone proposal and under plugins/vibe-editing/tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import distribution_safety as s

NOW = datetime(2026, 11, 11, 20, 0, tzinfo=timezone.utc)
A = "UC" + "A" * 22
B = "UC" + "B" * 22


def observation(d):
    return {"checked_at": NOW.isoformat(), "name": d.name, "can_write": True,
            "service": "youtube", "youtube_id": d.youtube_id, "buffer_id": d.buffer_id,
            "organization_id": d.organization_id, "disconnected": False, "locked": False,
            "paused": False, "mine_channel_ids": [d.youtube_id]}


def receipt(p):
    return {"id": p["remote_id"] or "synthetic-remote-1", "destination": p["destination_key"],
            "sha256": p["sha256"], "plan_id": p["plan_id"], "due_at": p["due_at"], "visibility": p["visibility"]}


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / "clip.mp4"
        self.file.write_bytes(b"synthetic A content")
        self.d = s.Destination("spawn-point", "Spawn Point Clips (SYNTHETIC)", A, "buffer", buffer_id="buffer-A", organization_id="org-A")
        self.ledger = s.Ledger(self.root / "history.sqlite3")
        self.ledger.initialize()
        self.provider = Mock(side_effect=receipt)
        gate = patch.object(s, "require_live_publishing")
        gate.start()
        self.addCleanup(gate.stop)
        network = patch.object(socket, "socket", side_effect=AssertionError("Network forbidden in synthetic tests"))
        network.start()
        self.addCleanup(network.stop)

    def plan(self, path=None, destination=None, asset="asset-A", **kwargs):
        path = path or self.file
        destination = destination or self.d
        intent = {"destination": destination.key, "sha256": s.full_hash(path)[0], "asset_id": asset}
        media = {"duration": 30.0, "audio": True, "width": 1080, "height": 1920} if destination.route == "buffer" else {"duration": 180.0, "audio": True, "width": 1920, "height": 1080}
        base = {"operation": "buffer_draft" if destination.route == "buffer" else "upload_private", "now": NOW, "media_probe": lambda _: media}
        base.update(kwargs)
        return s.prepare(path, destination, intent, "Synthetic title", "Full description\nSecond line — synthetic", **base)

    def send(self, p=None, d=None, **kwargs):
        d = d or self.d
        p = p or self.plan(destination=d)
        options = {"approve_id": p["plan_id"], "dry_run": False, "now": NOW, "emit": lambda _: None}
        options.update(kwargs)
        return s.exercise(p, d, observation(d), self.ledger, self.provider, **options)

    def another(self, name="other.mp4", content=b"different synthetic content"):
        p = self.root / name
        p.parent.mkdir(exist_ok=True, parents=True)
        p.write_bytes(content)
        return p

    def test_identical_filenames_different_bytes_are_distinct(self):
        p = self.another("second/clip.mp4")
        first, second = self.plan(), self.plan(p, asset="asset-B")
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.send(first)
        self.send(second)
        self.assertEqual(self.provider.call_count, 2)

    def test_renamed_identical_file_is_blocked(self):
        p = self.another("renamed.mp4", self.file.read_bytes())
        self.send()
        with self.assertRaisesRegex(s.Blocked, "Duplicate"):
            self.send(self.plan(p, asset="new-alias"))
        self.assertEqual(self.provider.call_count, 1)

    def test_same_video_different_channels_has_separate_history(self):
        other = replace(self.d, alias="tiny-detour", name="Tiny Detour Films (SYNTHETIC)", youtube_id=B, buffer_id="buffer-B")
        self.send()
        self.send(d=other)
        self.assertEqual(len(self.ledger.history(self.d.key)), 1)
        self.assertEqual(len(self.ledger.history(other.key)), 1)
        self.assertEqual(self.provider.call_count, 2)

    def test_alias_change_cannot_bypass_real_channel_identity(self):
        self.send()
        with self.assertRaises(s.Blocked):
            self.send(d=replace(self.d, alias="another-name"))
        self.assertEqual(self.provider.call_count, 1)

    def test_buffer_and_direct_route_share_duplicate_history(self):
        self.send()
        direct = replace(self.d, route="youtube", buffer_id="", organization_id="")
        with self.assertRaises(s.Blocked):
            self.send(d=direct)
        self.assertEqual(self.provider.call_count, 1)

    def test_rerender_with_same_asset_lineage_is_held(self):
        self.send()
        self.file.write_bytes(b"rerendered synthetic content")
        with self.assertRaisesRegex(s.Blocked, "Duplicate"):
            self.send()

    def test_file_hash_reads_beyond_first_64k(self):
        self.file.write_bytes(b"a" * 65536 + b"A")
        p = self.another("copy/clip.mp4", b"a" * 65536 + b"B")
        self.assertNotEqual(s.full_hash(self.file)[0], s.full_hash(p)[0])

    def test_manifest_wrong_channel_is_rejected(self):
        with self.assertRaisesRegex(s.Blocked, "manifest"):
            s.prepare(self.file, self.d, {"destination": "youtube:" + B}, "Title", "Description")

    def test_manifest_stale_hash_is_rejected(self):
        with self.assertRaisesRegex(s.Blocked, "stale"):
            s.prepare(self.file, self.d, {"destination": self.d.key, "sha256": "0" * 64, "asset_id": "A"}, "Title", "Description")

    def test_env_and_file_destination_conflicts_are_rejected(self):
        for opts in ({"legacy_env_id": "wrong"}, {"legacy_file_id": "wrong"}):
            with self.subTest(opts=opts), self.assertRaisesRegex(s.Blocked, "conflicts"):
                self.send(**opts)
        self.provider.assert_not_called()

    def test_observed_channel_must_match(self):
        p = self.plan()
        bad = {**observation(self.d), "youtube_id": B}
        with self.assertRaisesRegex(s.Blocked, "Wrong destination"):
            s.exercise(p, self.d, bad, self.ledger, self.provider, approve_id=p["plan_id"], dry_run=False, now=NOW, emit=lambda _: None)
        self.provider.assert_not_called()

    def test_buffer_org_profile_service_and_access_are_verified(self):
        changes = [{"organization_id": "wrong"}, {"buffer_id": "wrong"}, {"service": "instagram"},
                   {"disconnected": True}, {"locked": True}, {"paused": True}, {"can_write": False}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(s.Blocked):
                s.check_observation(self.d, {**observation(self.d), **change}, NOW)

    def test_oauth_mine_identity_must_be_unambiguous(self):
        d = replace(self.d, route="youtube")
        for ids in ([], [B], [A, B]):
            with self.subTest(ids=ids), self.assertRaises(s.Blocked):
                s.check_observation(d, {**observation(d), "mine_channel_ids": ids}, NOW)

    def test_stale_account_evidence_is_rejected(self):
        bad = {**observation(self.d), "checked_at": (NOW - timedelta(minutes=6)).isoformat()}
        with self.assertRaisesRegex(s.Blocked, "stale"):
            s.check_observation(self.d, bad, NOW)

    def test_missing_approval_never_calls_provider_or_writes_ledger(self):
        with self.assertRaisesRegex(s.Blocked, "approval"):
            self.send(approve_id=None)
        self.provider.assert_not_called()
        self.assertEqual(self.ledger.history(self.d.key), [])

    def test_changed_plan_field_invalidates_approval(self):
        p = self.plan()
        for field, value in (("title", "new"), ("description", "new"), ("visibility", "public"), ("due_at", "2030-01-01T12:00:00Z")):
            changed = {**p, field: value}
            with self.subTest(field=field), self.assertRaisesRegex(s.Blocked, "changed"):
                self.send(changed)
        self.provider.assert_not_called()

    def test_file_changes_even_with_same_size_and_mtime_are_rejected(self):
        p = self.plan()
        before = self.file.stat()
        self.file.write_bytes(b"synthetic B content")
        os.utime(self.file, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaisesRegex(s.Blocked, "Media changed"):
            self.send(p)
        self.provider.assert_not_called()

    def test_changed_route_configuration_is_rejected(self):
        p = self.plan()
        with self.assertRaisesRegex(s.Blocked, "configuration changed"):
            self.send(p, d=replace(self.d, buffer_id="different"))

    def test_dry_run_has_no_provider_calls_or_state_writes(self):
        before = self.ledger.path.read_bytes()
        mtime = self.ledger.path.stat().st_mtime_ns
        self.send(dry_run=True)
        self.provider.assert_not_called()
        self.assertEqual(self.ledger.path.read_bytes(), before)
        self.assertEqual(self.ledger.path.stat().st_mtime_ns, mtime)

    def test_dry_run_does_not_create_missing_ledger(self):
        self.ledger = s.Ledger(self.root / "never-created.sqlite3")
        self.send(dry_run=True)
        self.assertFalse(self.ledger.path.exists())

    def test_preview_shows_complete_required_fields(self):
        p = self.plan(operation="buffer_schedule", visibility="public", due_at="2026-11-12T12:00:00-05:00")
        card = json.loads(s.preview(p, observation(self.d)))
        self.assertEqual(card["destination"]["youtube_channel_id"], A)
        self.assertEqual(card["description"], p["description"])
        self.assertEqual(card["video"]["sha256"], p["sha256"])
        self.assertEqual(card["title"], p["title"])
        self.assertEqual(card["visibility_at_scheduled_delivery"], "public")
        self.assertEqual(card["schedule_utc"], "2026-11-12T17:00:00Z")
        self.assertEqual(card["schedule_local"], "2026-11-12T12:00:00-05:00")

    def test_unknown_duration_and_wrong_dimensions_fail_closed(self):
        for duration, width, height, audio in ((0, 1080, 1920, True), (float("nan"), 1080, 1920, True), (30, 1920, 1080, True), (30, 1080, 1920, False), (181, 1080, 1920, True)):
            with self.subTest(duration=duration, width=width, audio=audio), self.assertRaises(s.Blocked):
                self.plan(media_probe=lambda _: {"duration": duration, "width": width, "height": height, "audio": audio})

    def test_invalid_timezone_has_no_fallback(self):
        with self.assertRaisesRegex(s.Blocked, "timezone"):
            self.plan(destination=replace(self.d, timezone="Invalid/Zone"))

    def test_past_and_offsetless_schedules_are_rejected(self):
        for due in ("2020-01-01T00:00:00Z", "2026-11-12T12:00:00"):
            with self.subTest(due=due), self.assertRaises(s.Blocked):
                self.plan(operation="buffer_schedule", visibility="public", due_at=due)

    def test_expired_plan_never_submits(self):
        with self.assertRaisesRegex(s.Blocked, "stale"):
            self.send(now=NOW + timedelta(hours=2))
        self.provider.assert_not_called()

    def test_public_unscheduled_create_requires_new_explicit_operation(self):
        with self.assertRaises(s.Blocked):
            self.plan(visibility="public")

    def test_repeated_submission_calls_provider_once(self):
        p = self.plan()
        self.send(p)
        with self.assertRaisesRegex(s.Blocked, "Repeated"):
            self.send(p)
        self.assertEqual(self.provider.call_count, 1)

    def test_concurrent_claims_allow_one_provider_call(self):
        p = self.plan()
        start = threading.Barrier(6)
        def contender(_):
            start.wait()
            try:
                self.send(p)
                return "sent"
            except s.Blocked:
                return "blocked"
        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(contender, range(6)))
        self.assertEqual(outcomes.count("sent"), 1)
        self.assertEqual(self.provider.call_count, 1)

    def test_global_cap_applies_across_channels(self):
        self.send(global_cap=1)
        other = replace(self.d, youtube_id=B, buffer_id="buffer-B")
        with self.assertRaisesRegex(s.Blocked, "Global"):
            self.send(d=other, global_cap=1)

    def test_channel_cap_counts_uncertain_attempts(self):
        d = replace(self.d, daily_cap=1)
        self.provider.side_effect = TimeoutError
        with self.assertRaises(TimeoutError):
            self.send(d=d)
        with self.assertRaisesRegex(s.Blocked, "Channel"):
            self.send(self.plan(self.another(), destination=d, asset="B"), d=d)

    def test_timeout_after_remote_acceptance_is_unknown_and_cannot_retry(self):
        remote = []
        def interrupted(p):
            remote.append(receipt(p))
            raise TimeoutError("Lost acknowledgement")
        self.provider.side_effect = interrupted
        p = self.plan()
        with self.assertRaises(TimeoutError):
            self.send(p)
        with self.assertRaises(s.Blocked):
            self.send(p)
        self.assertEqual(len(remote), 1)
        self.assertEqual(self.ledger.history(self.d.key)[0]["status"], "unknown")

    def test_interruption_before_remote_call_is_also_held(self):
        p = self.plan()
        self.ledger.reserve(p, NOW)
        with self.assertRaises(s.Blocked):
            self.send(p)
        self.provider.assert_not_called()
        self.assertEqual(self.ledger.history(self.d.key)[0]["status"], "submitting")

    def test_ledger_failure_after_provider_ack_does_not_enable_retry(self):
        p = self.plan()
        with patch.object(self.ledger, "finish", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.send(p)
        with self.assertRaises(s.Blocked):
            self.send(p)
        self.assertEqual(self.provider.call_count, 1)

    def test_rate_limit_is_not_blindly_retried(self):
        self.provider.side_effect = RuntimeError("429 RATE_LIMIT")
        with self.assertRaises(RuntimeError):
            self.send()
        self.assertEqual(self.provider.call_count, 1)

    def test_bad_remote_acknowledgement_is_unknown(self):
        self.provider.side_effect = lambda p: {**receipt(p), "destination": "youtube:" + B}
        with self.assertRaises(s.Blocked):
            self.send()
        self.assertEqual(self.ledger.history(self.d.key)[0]["status"], "unknown")

    def test_reconciliation_records_evidence_without_resubmitting(self):
        p = self.plan()
        self.ledger.reserve(p, NOW)
        self.ledger.reconcile_confirmed(p, receipt(p), "Synthetic readback of matching immutable media and request")
        self.assertEqual(self.ledger.history(self.d.key)[0]["status"], "confirmed")
        self.provider.assert_not_called()
        with self.assertRaises(s.Blocked):
            self.send(p)

    def test_reconciliation_requires_matching_receipt_and_evidence(self):
        p = self.plan()
        self.ledger.reserve(p, NOW)
        for rec, evidence in ((receipt(p), ""), ({**receipt(p), "sha256": "wrong"}, "evidence")):
            with self.subTest(evidence=evidence), self.assertRaises(s.Blocked):
                self.ledger.reconcile_confirmed(p, rec, evidence)

    def test_missing_or_corrupt_history_cannot_be_reset_silently(self):
        for missing in (True, False):
            broken = self.root / ("missing.db" if missing else "broken.db")
            if not missing:
                broken.write_bytes(b"{interrupted")
            with self.subTest(missing=missing), self.assertRaises(s.Blocked):
                s.Ledger(broken).history(self.d.key)

    def test_repeat_on_a_new_day_still_blocks(self):
        self.send()
        p = self.plan(now=NOW + timedelta(days=1))
        with self.assertRaises(s.Blocked):
            self.ledger.reserve(p, NOW + timedelta(days=1))

    def remote(self, d):
        return {"id": "synthetic-remote-1", "etag": "snapshot-A", "snippet": {"channelId": d.youtube_id,
                "title": "Old title", "description": "Full description\nSecond line — synthetic", "categoryId": "20", "tags": ["gaming"], "defaultLanguage": "en"},
                "status": {"privacyStatus": "private", "license": "youtube", "embeddable": False, "publicStatsViewable": True, "containsSyntheticMedia": True}}

    def schedule(self, d, remote=None):
        remote = remote or self.remote(d)
        return self.plan(destination=d, operation="youtube_schedule", visibility="public", due_at="2026-11-12T17:00:00Z", remote_id=remote["id"], remote_snapshot=remote)

    def test_private_upload_then_schedule_same_video_is_allowed_once(self):
        d = replace(self.d, route="youtube")
        self.send(d=d)
        p = self.schedule(d)
        self.send(p, d=d)
        with self.assertRaises(s.Blocked):
            self.send(p, d=d)
        self.assertEqual(self.provider.call_count, 2)

    def test_scheduling_unadopted_existing_video_is_blocked(self):
        d = replace(self.d, route="youtube")
        with self.assertRaisesRegex(s.Blocked, "identity"):
            self.send(self.schedule(d), d=d)
        self.provider.assert_not_called()

    def test_youtube_schedule_preserves_description_tags_and_status(self):
        d = replace(self.d, route="youtube")
        remote = self.remote(d)
        body = s.youtube_schedule_body(self.schedule(d, remote), remote)
        self.assertEqual(body["snippet"]["description"], remote["snippet"]["description"])
        self.assertEqual(body["snippet"]["tags"], ["gaming"])
        self.assertEqual(body["snippet"]["categoryId"], "20")
        self.assertFalse(body["status"]["embeddable"])
        self.assertTrue(body["status"]["containsSyntheticMedia"])
        self.assertEqual(body["status"]["privacyStatus"], "private")
        self.assertEqual(body["status"]["publishAt"], "2026-11-12T17:00:00Z")

    def test_remote_metadata_change_requires_new_review(self):
        d = replace(self.d, route="youtube")
        p = self.schedule(d)
        remote = copy.deepcopy(p["remote_snapshot"])
        remote["snippet"]["description"] = "Someone edited this"
        with self.assertRaisesRegex(s.Blocked, "changed"):
            s.youtube_schedule_body(p, remote)

    def test_remote_video_owner_mismatch_is_rejected(self):
        d = replace(self.d, route="youtube")
        remote = self.remote(d)
        remote["snippet"]["channelId"] = B
        with self.assertRaisesRegex(s.Blocked, "video/channel"):
            self.schedule(d, remote)

    def test_buffer_defaults_to_private_draft_and_preserves_exact_text(self):
        p = self.plan()
        body = s.buffer_request(p, "https://media.invalid/hash")["input"]
        self.assertEqual(body["channelId"], self.d.buffer_id)
        self.assertTrue(body["saveToDraft"])
        self.assertEqual(body["metadata"]["youtube"]["privacy"], "private")
        self.assertEqual(body["text"], p["description"])
        self.assertNotIn("dueAt", body)

    def test_exact_buffer_schedule_is_bound_to_payload(self):
        p = self.plan(operation="buffer_schedule", visibility="public", due_at="2026-11-12T17:00:00Z")
        body = s.buffer_request(p, "https://media.invalid/hash")["input"]
        self.assertEqual(body["mode"], "customScheduled")
        self.assertFalse(body["saveToDraft"])
        self.assertEqual(body["dueAt"], p["due_at"])

    def test_direct_upload_body_is_private_with_description(self):
        body = s.youtube_upload_body(self.plan(destination=replace(self.d, route="youtube")))
        self.assertEqual(body["status"]["privacyStatus"], "private")
        self.assertEqual(body["snippet"]["description"], "Full description\nSecond line — synthetic")

    def test_storage_cache_is_scoped_to_bucket_and_full_hash(self):
        sha = s.full_hash(self.file)[0]
        first, key = s.storage_identity("https://storage.invalid", "bucket-A", "videos", sha)
        second, _ = s.storage_identity("https://storage.invalid", "bucket-B", "videos", sha)
        self.assertNotEqual(first, second)
        self.assertEqual(key, "videos/" + sha)

    def test_prehosted_url_requires_single_verified_immutable_asset(self):
        p = self.plan()
        for count, sha, immutable in ((2, p["sha256"], True), (1, "wrong", True), (1, p["sha256"], False)):
            with self.subTest(count=count, sha=sha, immutable=immutable), self.assertRaises(s.Blocked):
                s.check_prehosted(p, batch_count=count, observed_sha256=sha, immutable=immutable)


class ActualSyntheticMediaTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg/ffprobe required")
    def test_real_synthetic_video_probe_remux_and_lineage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, remux = root / "synthetic.mp4", root / "remux.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=90x160:r=2:d=10",
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000:duration=10",
                            "-c:v", "mpeg4", "-q:v", "15", "-c:a", "aac", "-y", str(first)], check=True, timeout=60)
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(first), "-c", "copy", "-metadata", "comment=synthetic-remux", "-y", str(remux)], check=True, timeout=30)
            self.assertNotEqual(s.full_hash(first), s.full_hash(remux))
            d = s.Destination("synthetic", "Synthetic", A, "buffer", buffer_id="buf-A", organization_id="org-A")
            def build(path):
                return s.prepare(path, d, {"destination": d.key, "sha256": s.full_hash(path)[0], "asset_id": "same-edit"},
                                 "Synthetic title", "Synthetic description", operation="buffer_draft", now=NOW)
            p1, p2 = build(first), build(remux)
            self.assertEqual(p1["media"]["width"], 90)
            self.assertTrue(p1["media"]["audio"])
            ledger = s.Ledger(root / "state.db")
            ledger.initialize()
            ledger.reserve(p1, NOW)
            with self.assertRaisesRegex(s.Blocked, "Duplicate"):
                ledger.reserve(p2, NOW)


if __name__ == "__main__":
    unittest.main(verbosity=2)
