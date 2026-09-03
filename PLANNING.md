# Reelforge — Planning

## Context
Reelforge turns any topic into a daily video, with no manual editing. The topic is a setting, not code — any RSS-based subject works. The pipeline is original: script → voice with word timestamps → matching stock footage per script segment → timed composite → captions → render.

Confirmed decisions: hosted live on Cloudflare, videos go up as **unlisted** first with a manual **Approve** step, real dashboard (topic list, run history, **Run Now** button), **no VPS/server purchase** — rendering runs free on GitHub Actions, and voice is **ElevenLabs**.

**Why the render step can't live in the Cloudflare Worker itself:** Workers only run JavaScript/Wasm — no FFmpeg binary, no long-running video encode. So the system has two halves, both original code.

## Architecture — two halves

**Half 1: Render engine — our own script, run by GitHub Actions (free)**
Our own Python script (`render.py`), triggered by a GitHub Actions workflow:
1. Take the story text + topic settings as input.
2. Call Claude → get back a script AND a list of segments, each tagged with an image search keyword (e.g. segment 1 = "ransomware attack").
3. Call ElevenLabs with the script, requesting word-level timestamps back.
4. Match each script segment to its real start/end time using those word timestamps → produces a timeline like "keyword A: 0:00-0:08, keyword B: 0:08-0:15."
5. Call Pexels/Pixabay for one image or clip per keyword.
6. Build an FFmpeg command from the timeline: lay out each image/clip for its exact time slot, burn in captions, mix in the voice track. Run FFmpeg (already installed on GitHub's runners) to render the final MP4.
7. Upload the MP4 to YouTube via the YouTube Data API, `privacyStatus: unlisted`.
8. Call back to the Cloudflare Worker's webhook with the result.

**Half 2: Control app (the dashboard you actually use)**
A Cloudflare Worker (TypeScript, Hono framework):
1. Serves the dashboard (a single web page).
2. Runs the daily Cron Trigger.
3. Holds the topic list and run history in **D1** (Cloudflare's SQL database).
4. Triggers the GitHub Actions workflow (via GitHub's API) with the story/topic settings to render.
5. Receives the callback from the render job when the video is done.
6. Handles the Approve step (flips the YouTube video from unlisted to public).

```
Your browser
      │
      ▼
Cloudflare Worker (dashboard + API + Cron)
      │
      ▼
   D1 (topics, runs)
      │
      ▼  (triggers via GitHub REST API)
GitHub Actions → runs our own render.py:
  Claude (script + keywords) → ElevenLabs (voice + timestamps)
  → Pexels/Pixabay (footage) → FFmpeg (composite + captions + render)
  → YouTube upload (unlisted)
      │
      ▼  (calls back with result)
Cloudflare Worker updates the run status → dashboard shows "Pending Approval"
```

## Data model (D1)
- `topics`: `id, name, rss_source, aspect_ratio, voice, active`
- `runs`: `id, topic_id, status (queued/rendering/uploaded/approved/failed), github_run_id, youtube_video_id, error_message, created_at`

## Workflow, end to end
1. **Trigger**: Cloudflare Cron fires daily, OR you click **Run Now** on a topic.
2. Worker pulls the top story from that topic's RSS feed.
3. Worker calls the GitHub Actions API (`workflow_dispatch`) to start `render.py`, passing the story text and the topic's settings.
4. Worker writes a `runs` row with status `queued` and the GitHub run ID.
5. `render.py` runs steps 2-7 above (script → voice+timestamps → matched footage → FFmpeg render → YouTube upload).
6. The job calls the Worker's webhook with the result (success + YouTube video ID, or failure + error message).
7. Worker updates the `runs` row to `uploaded` (or `failed`).
8. Dashboard shows it under "Pending Approval" with a preview link and an **Approve** button.
9. You click Approve → Worker calls the YouTube API, flips `privacyStatus` to `public`, updates the run to `approved`.

## Dashboard (the UI)
One page, three sections:
- **Topics** — table of topics (name, RSS source, active toggle), an "Add Topic" form, and a **Run Now** button per row.
- **Pending Approval** — finished-but-unlisted videos with a preview link and an Approve button.
- **History** — every past run with its status (or error, if something failed).

## Critical files
- `app/wrangler.toml` — Worker config: D1 binding, Cron schedule, secrets (GitHub token, YouTube keys, webhook shared secret)
- `app/schema.sql` — D1 tables (`topics`, `runs`)
- `app/src/index.ts` — Hono routes + cron handler + webhook receiver
- `app/src/pipeline.ts` — trigger + approve logic, shared by cron and manual trigger
- `app/src/clients/github.ts` — starts the Actions workflow, checks its status
- `app/src/clients/youtube.ts` — approve (privacy flip)
- `app/public/index.html` — the dashboard
- `render/render.py` — our own script: Claude → ElevenLabs+timestamps → footage matching → FFmpeg render → YouTube upload → webhook callback
- `render/.github/workflows/make-video.yml` — the Actions workflow that runs `render.py`

## One-time manual setup (before first run)
- Create the GitHub repo, add `render.py` and the workflow file.
- Add repo secrets: Claude API key, ElevenLabs API key, Pexels/Pixabay key, YouTube OAuth client + refresh token.
- Create a GitHub personal access token scoped to trigger workflows, store it as a Cloudflare Worker secret.
- Pick an RSS source for your first topic.
- Create the Cloudflare D1 database, bind it in `wrangler.toml`.

## Verification
1. `wrangler d1 execute` to apply `schema.sql`, insert one `topics` row for your first topic.
2. Run `render.py` once by hand (no Worker involved) with a sample story, confirm it produces a real MP4 and uploads it to YouTube as unlisted.
3. `wrangler dev` locally, click **Run Now**, confirm the Worker starts the Actions job and the run status moves `queued → uploaded` once the webhook comes back.
4. Confirm the video is on YouTube as unlisted, click **Approve**, confirm it flips to public.
5. `wrangler deploy` to go live, confirm the Cron Trigger is registered.
6. Add a second topic (different subject), run it manually — confirms no new code was needed, just a new row.
