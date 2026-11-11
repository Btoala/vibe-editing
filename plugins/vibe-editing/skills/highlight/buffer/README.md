# Buffer distribution review

`fill_queue.py` prepares channel-bound reviews for finished 9:16 shorts. **Live hosting,
queueing, scheduling and publishing are disabled in this revision.** Default invocation
and `--dry-run` preview the complete request; neither publishes.

Read [the distribution safety guide](../../../../../docs/DISTRIBUTION_SAFETY.md) for the
private registry, per-clip manifests, migration, approval and account-verification steps.

```bash
# From this directory: read-only account discovery (uses existing credentials).
python3 fill_queue.py discover

# Offline preview: registry + clip.mp4.publish.json must bind the intended channel.
python3 fill_queue.py fill --destination YOUR_ALIAS --dir /path/to/shorts --total 1 --dry-run

# Explicit local save for review; still no remote writes.
python3 fill_queue.py fill --destination YOUR_ALIAS --dir /path/to/shorts --total 1 \
  --save-plan /private/path/review.distribution-plan.json

# Uncommitted estimates, not actual Buffer queue reservations.
python3 fill_queue.py slots
```

The registry pins Buffer organization/profile IDs to the actual YouTube channel ID.
Conflicting `BUFFER_CHANNEL_ID` values block. Reuse the existing Buffer token and
Supabase settings; no new subscriptions are required. The current adapter supports
Supabase's API, not arbitrary S3 storage. `--video-url` accepts one clip and only its
configured immutable hash-addressed object, verified byte-for-byte before future use.

Each review shows destination IDs and verified/unverified name, video hash/asset ID,
full title/description, visibility, exact UTC/local schedule, hosting exposure and the
provider request. Clips must contain audio, last 10–180 seconds and be vertical 9:16.
Existing folder ordering and sidecar title/caption helpers remain available.

SQLite replaces filename-only duplicate history. Same bytes under new names remain
blocked for the same channel; different channels have separate histories. A stable
editorial asset ID also catches re-renders of the same clip. Hashing is not perceptual
matching. Pending/unknown operations block automatic retries. Global and per-channel
attempt caps are enforced transactionally; old logs are preserved and require explicit
mapping, never silently discarded.

Default plans are saved drafts with private delivery settings. **Buffer still needs
publicly fetchable source media**, which is separately gated. Scheduled plans require an
explicit timestamp and visibility. Existing Buffer drafts are promoted by editing the
same post ID. Provider acceptance and actual YouTube publication are separate outcomes.

Use `--verify` for read-only connected identity checks; it cannot be combined with
`--dry-run`. Even exact-plan approval cannot enable live operations until a separate
reviewed release removes the hard gate after connected-account validation.
