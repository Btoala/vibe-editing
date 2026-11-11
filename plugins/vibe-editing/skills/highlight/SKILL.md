---
name: highlight
description: THE highlights-channel clipper, run three ways. MINE mode (given a link or file of a raw Q&A / hotline / podcast recording) mines it into RANKED, postable HORIZONTAL 16:9 "mid" videos optimized for SUBSCRIBER growth (subs_per_1k_views, NOT views/CTR), cuts them, optionally appends YOUR own CTA outro, and delivers them locally to upload as YouTube drafts. POST mode (after you have uploaded those mids as drafts and say "post & schedule these") prepares channel-bound title and schedule reviews using your own YouTube sign-in for optional read-only checks; live publishing is disabled pending account verification. FILL mode (Mode C) prepares manifest-bound distribution reviews for finished 9:16 SHORTS from a folder using the Buffer API + your own storage; live writes are disabled (MINE/POST make 16:9 mids; /edit makes the 9:16 shorts that FILL distributes). Trigger keywords highlight, /highlight, mine mids, make mids, highlights channel, Q&A mid, hotline mid, raw Q&A to clips, mine this hotline, post and schedule these, schedule these out, go post these, title and schedule the drafts, fill my queue, fill the buffer queue, auto-post shorts, queue shorts to buffer, schedule shorts.
---

# /highlight — highlights-channel clipper

Turn a long recording (Q&A / hotline / podcast) into ranked, postable **HORIZONTAL 16:9
"mid" videos** for a highlights / clips channel whose goal is **subscriber growth**. Three
modes: **MINE** (recording → finished mids), **POST** (explicit uploaded drafts → title/schedule review), and **FILL** (Mode C — review finished 9:16 shorts for Buffer distribution). Live publishing is disabled in this revision.

> **Mids are 16:9 horizontal**, ~3–10 min — regular videos, not 9:16 shorts. (For shorts use
> `/edit`.) This skill self-locates via `${CLAUDE_PLUGIN_ROOT}` (the plugin install dir); every
> script finds the plugin root automatically, so nothing is hardcoded to a home folder.

## 🔒 NON-NEGOTIABLES — verify before EVERY delivery, never silently skip
1. **Optimize selection + titles for subscribers (`subs_per_1k_views`), never raw CTR.** The
   whole point of a highlights channel is to convert the *right* viewer into a subscriber.
2. **16:9 horizontal; the cut ends the instant the payoff lands — nothing trails.**
3. **CTA outro is OPTIONAL and user-supplied.** If you drop your own outro clip at
   `brand/cta/outro.mp4` (repo root), `highlight_cut.py` appends it to every mid. No file →
   it skips gracefully. (`--cta <path>` overrides the location; `--no-cta` forces skip.)

**KPI = subscribers (`subs_per_1k_views`), not views/CTR.** Mids are **16:9, ~3–10 min.**

## Three modes — detect which from what the user says
- **MINE** — they give a **link/file** to a raw recording / say "mine these". → produce
  finished mids, deliver locally. **STOP there** — they upload them as drafts themselves.
- **POST** — they say **"post & schedule these" / "schedule these out"** (the mids are already
  uploaded as YouTube drafts on their channel). → prepare an exact title/schedule review for the selected IDs.
- **FILL (Mode C)** — they say **"fill my queue" / "auto-post my shorts"**. → queue finished
  9:16 shorts from a folder into a destination-bound Buffer review (see Mode C below).

The full loop: `MINE → (you upload drafts) → POST → (Studio re-export feeds back to sharpen).`

---

## MODE A — MINE  (recording → finished mids)

```
[1] TRANSCRIBE  word-level + diarized (AssemblyAI primary, labeled fallback evidence)
                → highlight_diarize labels Spk0=Host, Spk1=Guest/Caller (no names)
[2] FORMAT      highlight_miner.py format <transcript> --out <work>/miner_input.txt
[3] MINE        Claude segments miner_input.txt → _segments.json (references/cutting_prompts.md STAGE 1)
[4] SCORE+TITLE highlight_miner.py score --segments _segments.json --out-dir <work>/out  → review.md
[5] REVIEW      present review.md; the user picks which to build (post if score ≥ 55)
[6] CUT (+CTA)  highlight_cut.py --src <rec> --words <words.json> --keep '[[a,b],...]' --out <mid>.mp4
                → precision-cut 16:9, then appends brand/cta/outro.mp4 IF it exists. Emits clip.contract.json.
[7] DELIVER     finished mids → your project's delivery folder
```
**Then STOP and hand the mids over.** The user uploads them to YouTube Studio as drafts. Do
NOT auto-upload (the bridge is optional — see bottom).

Mining judgment lives in: `references/cutting_prompts.md` (how Q&A/hotline gets cut),
`references/selection_rules.md` (the scored fields Claude fills), `references/title_rules.md`
(subs-optimized titles — lead with You / quoted-Q / Helping; keep quote marks on quoted
questions; never optimize for CTR).

### Transcription (this kit's standard)
→ Use skill: `long-form-ingest` for AssemblyAI-first transcript evidence and the source listener.
For clip-local caption words the bundled `caption-clips/scripts/transcribe_lv3.py` now selects
AssemblyAI when connected. Keep `GROQ_API_KEY` for audit/fallback helpers, not primary onboarding.

---

## MODE B — POST ("post & schedule these")

Prepare a review for explicitly selected private YouTube uploads. Read
[distribution safety](../../../../docs/DISTRIBUTION_SAFETY.md) before preparing any upload,
queue or schedule. Live media writes are disabled; do not bypass the release gate.

Require the private destination registry, a hash/channel-bound manifest beside each
local video, and `--selection` naming the exact local file and existing video ID. Never
select all private uploads automatically. Use an offline provider snapshot for dry-run,
or request `--verify` for read-only identity and metadata retrieval.

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/highlight/scripts/highlight_post.py \
  --destination YOUR_ALIAS --selection selection.json --snapshot snapshot.json \
  --no-retitle --dry-run
```

Display the complete title, description, hash, destination IDs, visibility, exact local
and UTC schedule, and provider request before any publishing operation. Explain that
YouTube `private + publishAt` becomes **public at the scheduled time**. Preserve unrelated
metadata. Reuse existing OAuth tokens; do not silently open a login flow, change accounts
or overwrite grants. The local titler remains available; paid titling requires explicit
`--use-llm` and cannot run during dry-run. A saved plan and matching approval digest do
not bypass the currently disabled live gate.

## MODE C — FILL ("fill my queue" / "auto-post my shorts")

Prepare Buffer reviews for finished **9:16 shorts**. Distribution only; editing stays
unchanged. Require an explicit destination registry and each clip's `.publish.json`
manifest. Treat mismatched environment/file destination configuration as a blocker.

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/highlight/buffer/fill_queue.py discover
python3 ${CLAUDE_PLUGIN_ROOT}/skills/highlight/buffer/fill_queue.py fill \
  --destination YOUR_ALIAS --dir /path/to/shorts --total 1 --dry-run
```

`discover` reads the connected Buffer account; dry-run is offline and writes nothing.
Default `fill` also previews. Validate audio, duration (10–180 seconds), 9:16 dimensions,
full-file SHA-256 and asset lineage. Histories are scoped to canonical YouTube IDs and
shared across direct/Buffer routes; caps include uncertain attempts. Renames cannot
bypass exact-byte detection, but hashes alone do not detect re-encodes.

Buffer saved drafts default to private delivery. Hosting the source video would still
make its bytes publicly fetchable, so hosting also requires the approved plan and live
release gate. Reuse the existing Buffer credentials and Supabase configuration. Do not
introduce a service or assume a draft proves eventual publication. See
[buffer/README.md](buffer/README.md) and the shared distribution guide for setup,
legacy migration, approval and recovery limitations.

---

## Guardrails (hard)
- Optimize for **subscribers**, not views/CTR. Curiosity/"Why" titles only on an explicit reach play.
- Cut **opens on the guest/caller's problem** (with numbers), **ends the instant the payoff
  lands** — nothing trails.
- 16:9 horizontal. No 9:16 reframe. Keep it brand-safe (no prices-as-promises, no personal PII).
- **No clear payoff = not postable.** Publishing requires full-plan review; live mutations remain disabled.

## Files
- `references/`: `research_findings.md` (the method's empirical basis) · `cutting_prompts.md`
  + `segmenter_prompt.ts` + `clipper_hookmeatpayoff.ts` (the segment/clip prompts) ·
  `selection_rules.md` · `title_rules.md` · `feedback_loop.md`
- `config/patterns.json` — title-pattern → subscriber weights (the method; tune on YOUR data).
- `scripts/`: `highlight_miner.py` (format+score) · `highlight_cut.py` (cut + optional CTA) ·
  `highlight_cta.py` (append optional outro) · `highlight_post.py` (POST: title+schedule via
  YouTube API) · `title_sop.py` (the titler) · `highlight_diarize.py` (host/guest labels) ·
  `highlight_asd.py` + `highlight_multicam.py` + `highlight_reframe16.py` (two-cam Q&A
  switching) · `highlight_studio_import.py` (import YOUR Studio CSV to retune weights) ·
  `highlight_source_match.py` (link a mid back to its long-form source) ·
  `highlight_publish_bridge.py` (private-upload review; live upload disabled).
- `buffer/` — **MODE C (FILL)**: review finished 9:16 shorts for your Buffer queue
  (`fill_queue.py` — bring your own Buffer token + channel + storage). See `buffer/README.md`.

## Reuse, don't duplicate (ONE-skill rule)
For **editorial cut quality**, lean on `/edit`'s Q&A standards rather than re-deriving them.
This skill owns mining + boundary-picking for mids; it POINTS to the shared engines
(`lib/_shared/precision_cut.py`, `fast_encode.py`) and to `/edit` for the cut SOP.

## Tune it on YOUR channel (the feedback loop)
Selection + title weights ship as starter defaults. To make them yours: export your YouTube
Studio table (`Content / Views / Impressions CTR / Subscribers / …` as `Table data.csv`) and
import it to measure your real `subs_per_1k_views`, then re-rank `config/patterns.json`:
```
python3 ${CLAUDE_PLUGIN_ROOT}/skills/highlight/scripts/highlight_studio_import.py \
    --csv ~/Downloads/"Table data.csv" --out <work>/studio.json   # preview; add --write-db to persist
```
That closes the loop: post → measure your subscriber conversion → re-weight → mine better.
