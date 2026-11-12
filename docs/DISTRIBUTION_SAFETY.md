# Multi-channel distribution review

**Live media writes are disabled in this revision.** Default commands and `--dry-run`
prepare reviews. A saved plan and matching approval digest still cannot upload, host,
queue, schedule or publish media. `require_live_publishing()` has no CLI or environment
override. Releasing that gate requires a separate reviewed change after account checks.

Contents: [changes](#what-changed), [setup](#private-configuration),
[review workflow](#review-workflow), [history](#history-and-interruption-recovery),
[validation](#validation-and-release-conditions).

## What changed

| Area | Previously | This revision |
| --- | --- | --- |
| Clip identity | Buffer filename stem | Full-file SHA-256 plus stable editorial `asset_id` |
| Destination | Separate environment/file selector | Clip manifest, registry and approved plan bind the canonical YouTube channel ID |
| History | Shared filename-only JSON; bridge log written after upload | SQLite transactions, separate channel histories, shared identity across direct YouTube and Buffer routes |
| Approval | Buffer normal path created public posts | Full review by default; exact saved request and approval digest; live gate remains closed |
| Interruptions | Remote acceptance could precede durable local history | Durable reservation before send, early remote-ID/session checkpoints, unknown outcomes block repeats |
| Scheduling | Broad selection of private uploads; metadata replacement | Explicit video IDs, preserved metadata, exact timestamp, slot conflicts checked before send |
| Media hosting | Filename/URL cache and overwritable objects | Full-hash object names, no upsert, byte verification; hosting remains gated |

Editing, rendering, captions, transcription and the launcher are unchanged. Existing
credential files remain usable; no credentials are copied into plans or committed. No
new service or subscription is introduced. SQLite is part of Python. Existing FFmpeg,
Google client libraries, requests, optional Buffer account and existing storage suffice.
The original local titler is preserved; paid titling now requires explicit `--use-llm`.

SHA-256 detects identical **bytes**, including renamed files. It does not identify every
re-encode, crop or near-duplicate. Keep the same `asset_id` when revising one editorial
clip so a re-render also triggers a review hold. This is not a perceptual fingerprint.

## Private configuration

Start with `plugins/vibe-editing/config/distribution.example.json`; replace every
placeholder with verified IDs and save privately at
`~/.config/vibe-editing/distribution.json` (or use `--registry`). The example cannot run
unchanged. Names are labels; only exact platform IDs establish the destination.

Create a registry entry for each route you actually use. Two routes to the same YouTube
channel must share the same `youtube_id`; all configured routes are checked for occupied
schedule times. Separate channels need separate YouTube OAuth grants if the existing
grant authorizes only one channel. Reuse those token files through `youtube_token_path`;
the default remains the kit's `config/youtube_token.json`. Verification refreshes an
existing grant in memory only, never opens a login flow or overwrites a token. Missing
grants/scopes require explicit connection setup outside this review command.

Buffer keeps the existing token loader (`BUFFER_TOKEN` / existing config). Its profile
ID, organization ID and platform `serviceId` must all match. Conflicting
`BUFFER_CHANNEL_ID` environment or local-file values block instead of overriding the
manifest. Remove an obsolete global selector when using multiple profiles. Reusable
credentials do not imply interchangeable destination channels. `credential_profile`
is a review label; it does not select a different secret store.

For Buffer media hosting, registry storage origin/bucket/prefix must match the existing
Supabase configuration. The implementation supports the existing Supabase adapter,
not an arbitrary S3 API. Pre-hosted URLs are limited to one clip and the configured
hash-addressed object, with a full-byte check before use. A Buffer draft still requires
publicly fetchable source media; **draft does not mean private source hosting**.

Each finished video needs a sibling manifest, for example `clip.mp4.publish.json`:

```json
{
  "schema": 1,
  "destination": "youtube:REPLACE_WITH_VERIFIED_CHANNEL_ID",
  "sha256": "REPLACE_WITH_FULL_FILE_SHA256",
  "asset_id": "spawn-point-episode-017-clip-03",
  "title": "The exact reviewed title",
  "description": "The complete reviewed description",
  "visibility": "private",
  "made_for_kids": false,
  "notify_subscribers": false
}
```

Compute the file hash with `sha256sum clip.mp4` (or `shasum -a 256` on macOS).
Set audience and notification choices deliberately. Scheduling requires an explicit
future `publish_at` with UTC offset and the intended visibility. No filename-to-channel
guessing or automatic fan-out occurs. Cross-posting requires a separately bound plan
for each intended destination, with its own channel history.

## Review workflow

Run commands from the repository root. These examples use configured aliases; choose
the correct one for your installation.

```bash
# Offline Buffer review: no credential loading, network or state writes.
python3 plugins/vibe-editing/skills/highlight/buffer/fill_queue.py fill \
  --destination spawn-point-buffer --dir /path/to/finished-shorts --total 1 --dry-run

# Offline private YouTube upload review (existing horizontal mids workflow).
python3 plugins/vibe-editing/skills/highlight/scripts/highlight_publish_bridge.py \
  --destination tiny-detour-youtube --video /path/to/mid.mp4 --dry-run

# Explicitly save a private review packet; this is a local file write only.
python3 plugins/vibe-editing/skills/highlight/buffer/fill_queue.py fill \
  --destination spawn-point-buffer --dir /path/to/finished-shorts --total 1 \
  --save-plan /private/path/review.distribution-plan.json
```

Every preview displays configured/verified destination names, canonical YouTube ID,
Buffer IDs where applicable, full hash, asset ID, file path, duration/dimensions/audio,
full title and description, current/future visibility, UTC/local schedule, audience,
notifications, hosting exposure and the exact provider request. Offline identity is
marked **UNVERIFIED**. Missing history is shown as blocked, never treated as empty.
Plans expire after one hour. Changed bytes, destination settings, metadata or schedule
invalidate the approved digest. A digest binds values; it is not proof of human consent.

Use `--verify` separately for explicit read-only identity checks. Do not combine it with
`--dry-run`. `discover` also makes read-only Buffer requests. Default and dry-run paths
make none. `--save-plan` and `--execute-plan` cannot be combined with dry-run.

For already uploaded private YouTube videos, provide `--selection selection.json`
containing an explicit list:

```json
[
  {
    "video_path": "/path/to/mid.mp4",
    "video_id": "EXACT_EXISTING_VIDEO_ID",
    "publish_at": "2030-01-02T12:00:00-05:00"
  }
]
```

The manifest must also bind `youtube_video_id`. Supply `--snapshot snapshot.json`
containing `{ "channel_id": "UC...", "videos": [...] }` with complete `videos.list`
`snippet,status` responses for offline review, or use `--verify` for read-only retrieval.
Scheduling also requires a confirmed local upload record or explicit adoption below.
`--no-retitle` preserves the existing title; unchanged title/description sends only
`part=status`. Changed titles preserve supported mutable snippet/status fields.

```bash
python3 plugins/vibe-editing/skills/highlight/scripts/highlight_post.py \
  --destination tiny-detour-youtube --selection selection.json \
  --snapshot snapshot.json --no-retitle --dry-run
```

YouTube `private + publishAt` means **public publication at the specified time**, not a
permanently private draft. This is displayed explicitly. Generated candidate slots use
the registry timezone, skip occupied times and DST gaps, and reject ambiguous repeated
hours. Buffer's `slots` command is only an estimate, never a reservation. An existing
Buffer draft is promoted using `editPost` with the same post ID and snapshot, not
recreated; bind `buffer_post_id`, `buffer_snapshot`, `publish_at` and `visibility` in its
manifest. Actual remote state is fetched again immediately before a future approved send.

The future execution interface is `--execute-plan PATH --approve-plan-id BATCH_DIGEST`.
It currently stops at the unconditional release gate. Old `--yes-upload` and direct
legacy mutation helpers cannot bypass that gate. Do not remove the gate to run tests.

## History and interruption recovery

Stop old publishing jobs before migration. Then explicitly initialize local state:

```bash
python3 plugins/vibe-editing/distribution_runtime.py init-state
python3 plugins/vibe-editing/distribution_runtime.py history --channel-id VERIFIED_UC_ID
```

Default state is `~/.local/state/vibe-editing/distribution/history.sqlite3`, mode 0600.
Use one authoritative local database for all workers/routes, back it up consistently,
and keep it outside Git. Separate databases or installations cannot coordinate
duplicates. This does not introduce a hosted database or claim multi-machine locking.

Initialization preserves and backs up existing `posted_log.json`, `url_cache.json`,
`api_calls.json` and bridge JSONL records. It retains legacy daily usage, quarantines
unattributed history, and refuses corrupt history. The completed database becomes
available atomically; interrupted initialization cannot expose a partial ledger.
Missing stores do not silently regenerate. Subsequent changes to old histories block
new reservations. URL cache entries are never proof of prior posting or content identity.

Old filename-only records cannot safely infer either content or destination. To adopt
a known remote video, review the content yourself and use the local `adopt` command with
`--destination`, `--video`, `--remote-id`, `--confirm-sha256` and `--evidence`. It performs
read-only ownership checks and records your explicit content mapping. To resolve a
legacy hold, also provide its exact `--legacy-source` and `--legacy-key`. Providers do
not return the original SHA-256; adoption is a human attestation, not a verified remote
hash. Unresolvable historical records keep distribution blocked. Do not delete history
to get around a hold.

Each operation reserves its channel/hash/asset and schedule slot in one SQLite
transaction before any external write. Same-name different bytes are distinct; renamed
identical bytes are blocked for the same channel; a different channel has independent
history. Direct YouTube and Buffer routes to the same channel share this guard. Global
and per-channel UTC-day attempt caps count pending/uncertain attempts, including upload
and scheduling as separate operations. A batch is not an all-or-nothing remote transaction.

A timeout or process loss leaves `unknown` or `submitting`; both block automatic repeats.
Provider acknowledgements are stored before readback. YouTube resumable session URLs
are checkpointed before the first chunk, kept private and omitted from history output.
Recovery must inspect provider state and reconcile the original operation; this revision
does not expose an automatic retry/resume command. An accepted Buffer post is not proof
that YouTube eventually published it. Buffer exposes no assumed idempotency guarantee;
if acceptance cannot be established, hold the operation instead of recreating it.

## Validation and release conditions

Run the existing and new offline suite:

```bash
python3 -m unittest discover -s plugins/vibe-editing/tests -v
```

Tests use synthetic bytes/media, temporary private state and fake providers. Network is
blocked in integration tests; a test-only monkeypatch exercises guarded dispatch without
enabling production writes. Cases cover filename collisions, renames, re-renders with
shared asset identity, cross-channel and cross-route history, wrong configuration,
approval/payload changes, concurrent reservations, interruption/repetition, missing or
corrupt state, migration, caps, schedule conflicts and metadata preservation.

Before any release, real connected accounts must establish: correct OAuth/Brand channel
and scopes; correct Buffer organization/profile/service ID and action permissions;
complete pagination; exact current GraphQL fields and draft/edit behavior; storage
permissions and byte-stable hosting; resumable upload status/recovery; YouTube metadata
preservation and conditional-update behavior; schedule/timezone/visibility readback and
eventual delivery. Read-only checks can establish identity and permissions, but cannot
prove successful future writes. Provider write testing needs separate explicit approval
and controlled test media. No such calls were made for this implementation.

Remaining limits: perceptual duplicates, manually posted videos missing from the local
ledger, external edits racing the final read, other machines using separate state, and
provider-side eventual delivery. No local script can promise exactly-once publication
across uncoordinated provider/manual operations. Keep the gate closed until these
operational boundaries and connected contracts are reviewed.

API references: [YouTube update](https://developers.google.com/youtube/v3/docs/videos/update),
[YouTube channel ownership](https://developers.google.com/youtube/v3/docs/channels/list),
[Buffer PostInput](https://developers.buffer.com/types/PostInput.html),
[Buffer EditPostInput](https://developers.buffer.com/types/EditPostInput.html),
[Buffer channel actions](https://developers.buffer.com/types/ChannelAction.html).
