# Video2Slides

Convert an MP4 video into an editable, professionally designed PowerPoint deck.

```
MP4 upload ─► FFmpeg (audio 16 kHz mono + key frames) ─► faster-whisper (local CPU)
          ─► Gemini (structured outline, JSON schema) ─► python-pptx design engine ─► .pptx download
```

* **Single process**: FastAPI serves the API and a plain HTML/JS frontend. No database, Redis or
  external storage — job state is JSON on disk.
* **Free/open-source by default**: FFmpeg, faster-whisper and python-pptx run locally. The only
  external call is Gemini (free tier), and the app still works without it in a reduced offline mode.

## Features

| Area | What it does |
|---|---|
| Upload | Drag & drop, client-side size/duration check, server-side re-validation with ffprobe |
| Video | Validates container/streams/limits, extracts mono 16 kHz WAV, samples frames and drops black, blank and duplicate frames (perceptual hash + colour) |
| Speech | faster-whisper `tiny`/`base`/`small`, CPU int8, VAD, timestamps; model loaded once per process, one transcription at a time |
| AI | Gemini REST with `responseSchema`, pydantic validation, self-repair on invalid JSON, backoff on 429/5xx; provider interface for adding Groq etc. |
| Design | 3 themes (Professional, Academic, Creative) × 11 layouts: title, section, bullets, image-left, image-right, two-column, process, timeline, cards, stats, summary |
| Output | 16:9, native editable text and shapes, embedded video frames, speaker notes with source timestamps, overflow prevention, post-build validation |
| Jobs | Stage-based progress, retry reusing cached transcript/outline, auto-cleanup after `JOB_TTL_HOURS` |
| Security | Server-issued job IDs only (no user paths), no `shell=True`, upload size cap while streaming, optional shared password (`APP_PASSWORD`), API key sent in a header (never in URLs/logs) |

## Project layout

```
app/
  main.py            FastAPI routes + static frontend
  config.py          all limits/settings from environment variables
  jobs.py            file-backed job store + single worker thread
  pipeline.py        stage orchestration with caching
  video.py           ffprobe/ffmpeg helpers
  transcribe.py      faster-whisper wrapper
  ai/                schema, prompt, Gemini provider, offline fallback
  url_import/        YouTube/Facebook URL parsing, SSRF guard, metadata, bounded retrieval
  pptx_engine/       themes, text fitting, layouts, validation
  static/            index.html, app.js, styles.css
scripts/download_model.py   pre-download Whisper weights
tests/                      pytest suite
```

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | ffmpeg/ffprobe availability |
| `GET` | `/api/config` | limits, styles, languages, active AI provider |
| `POST` | `/api/jobs` | multipart upload (`file`, `title`, `language`, `style`, `slide_count`) → `202 {job_id}` |
| `GET` | `/api/jobs/{id}` | status, current stage, stage timings, result/error |
| `POST` | `/api/jobs/{id}/retry` | re-run a failed job (cached steps reused) |
| `GET` | `/api/jobs/{id}/transcript` | `transcript.json` |
| `GET` | `/api/jobs/{id}/transcript.txt` | readable transcript |
| `GET` | `/api/jobs/{id}/download` | the `.pptx` |
| `DELETE` | `/api/jobs/{id}` | delete a finished job and its files |
| `POST` | `/api/videos/url/validate` | `{url}` → platform, title, thumbnail, duration, `status`, `retrieval_permitted`, Vietnamese `message` |
| `POST` | `/api/videos/url/import` | `{url, rights_confirmed, title, language, style, slide_count}` → `202 {job_id}`; `403` when retrieval is not permitted |

When `APP_PASSWORD` is set, every `/api/jobs*` call needs the header `X-App-Password`.

## Configuration

Copy `.env.example` to `.env` for local development. On Replit use **Secrets** instead (never
commit keys). Main variables:

| Variable | Default | Notes |
|---|---|---|
| `GEMINI_API_KEY` | – | Free key from Google AI Studio (see below). Without it the app uses the offline outline. |
| `GEMINI_MODEL` | `gemini-flash-latest` | Any model your key can call, e.g. a specific Flash / Flash-Lite version |
| `AI_PROVIDER` | `auto` | `auto`, `gemini`, `offline` |
| `WHISPER_MODEL` | `base` | `tiny` / `base` / `small` |
| `DEFAULT_LANGUAGE` | `vi` | `vi`, `en`, `auto` |
| `MAX_UPLOAD_MB` / `MAX_DURATION_SEC` / `MAX_FRAMES` | 80 / 600 / 8 | MVP limits |
| `MAX_QUEUE_SIZE` | 3 | uploads beyond this get HTTP 429 |
| `JOB_TTL_HOURS` | 24 | finished jobs are deleted afterwards |
| `APP_PASSWORD` | – | optional shared password for the whole app |

### Getting a Gemini API key

1. Open <https://aistudio.google.com/apikey> and sign in with a Google account.
2. Click **Create API key** (a free-tier project is created automatically).
3. Put it in Replit → **Tools → Secrets** as `GEMINI_API_KEY` (or in your local `.env`).
4. Restart the app. The header badge changes from "AI: offline" to "AI: Gemini".

Free-tier quotas (requests/minute and per day) change over time and depend on the model;
check your project's limits in AI Studio. The app makes **one** Gemini request per video
(plus at most two repair/retry requests on errors).

## URL import (YouTube / Facebook)

```
URL ─► strict parse + domain allowlist ─► official metadata (oEmbed / Data API / Graph API)
    ─► policy: retrieval permitted? ─► (user confirms rights) ─► bounded download (≤480p, ≤80 MB, ≤10 min)
    ─► same pipeline as uploads: ffprobe validate ─► FFmpeg ─► faster-whisper ─► AI ─► PPTX
```

Code: `app/url_import/` (`urls.py` parsing + SSRF guard, `metadata.py`, `retrieval.py`, `service.py`).

Accepted links: `youtube.com/watch?v=`, `/shorts/`, `/embed/`, `/live/`, `youtu.be/`; Facebook
`/watch/?v=`, `/{page}/videos/…/{id}`, `/reel/{id}`, `fb.watch/…`, `/share/v|r/…`. Playlists,
channels, profiles and any other host are rejected; a `list=` parameter is dropped.

### Compliance — why retrieval is OFF by default

| Platform | Metadata | Automatic media retrieval |
|---|---|---|
| YouTube | Public oEmbed (title, channel, thumbnail). Duration/age/live checks with optional `YOUTUBE_API_KEY` | **Off** (`YOUTUBE_RETRIEVAL=off`). YouTube's Terms of Service forbid downloading except through features YouTube provides or with written permission; the YouTube API Services do not offer media download. Owners can download their own video in YouTube Studio and upload the MP4. |
| Facebook | Graph `oembed_video` + video node, needs `FACEBOOK_ACCESS_TOKEN` | `graph_api` (official): enabled only when the token can read the video's `source` — in practice videos of Pages you manage with a Page token. Otherwise not permitted. Meta's Terms forbid automated collection without permission. |

`ytdlp` mode exists for operators who have **explicit permission** (e.g. a written agreement, or
content you own where you accept the platform-terms risk). It is never enabled automatically, and
`retrieval_permitted` is never set just because yt-dlp could technically fetch a video. In every
mode the user must tick "I own this content or have permission" before import, and no cookies,
logins, proxies, CAPTCHA/bot-check bypass or DRM circumvention are used. When retrieval is not
permitted the UI explains why and offers MP4 upload.

### Security measures

* Host allowlist + path shape per platform (no generic downloader, no IP literals, no userinfo,
  ports 80/443 only, control characters rejected, look-alike/IDN hosts rejected).
* Every server-side request (`check_outbound`): https only, host allowlisted for that platform,
  DNS must resolve only to public IPs (blocks loopback, RFC1918, link-local/`169.254.169.254`,
  CGNAT, IPv6 ULA, IPv4-mapped IPv6).
* Redirects followed manually one hop at a time (max 5), each target re-validated.
* yt-dlp: Python API, only the `youtube` / `facebook` / `facebook:reel` extractors, `noplaylist`,
  no cookies/config, plain-https formats only (no ffmpeg-fetched HLS), and its `urlopen` is
  overridden so every request — extractor, media, fragments — passes `check_outbound`, and the
  final URL after redirects is checked too.
* Limits enforced before (metadata, size estimate) **and** during download (actual byte count,
  wall-clock deadline); then the file is re-validated with ffprobe like an upload.
* Fixed output names inside a per-job `download/` directory, always deleted; source video deleted
  after success. Signed media URLs and tokens never reach the browser or the logs (tokens sent as
  headers; `httpx` request logging disabled; yt-dlp messages truncated).
* Concurrency: `MAX_CONCURRENT_DOWNLOADS` (default 1) download threads; transcription stays at 1
  (single processing worker + lock). Total waiting jobs bounded by `MAX_QUEUE_SIZE` (HTTP 429).
* Optional per-IP rate limit `URL_RATE_LIMIT_PER_MIN` and the existing `APP_PASSWORD` gate
  (both off by default — see limitations).

Residual risk: an allowlisted host (e.g. a CDN) redirecting to an internal address is caught when
the redirect is followed, but the DNS check and the actual connection are separate lookups
(theoretical DNS-rebinding window on allowlisted platform domains).

## Run locally

Requirements: Python 3.11–3.12, FFmpeg on `PATH`.

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv -e ".[dev]"
.venv/Scripts/python scripts/download_model.py          # Windows; use .venv/bin/python on Linux/macOS
.venv/Scripts/python -m uvicorn app.main:app --port 8000
```

Open <http://localhost:8000>.

### Tests

```bash
.venv/Scripts/python -m pytest -q
```

The real-speech test is opt-in because it loads the Whisper model:

```bash
V2S_REAL_VIDEO=path/to/vietnamese.mp4 .venv/Scripts/python -m pytest -m slow
```

## Run on Replit

1. Import the GitHub repository into Replit (Python template).
2. `.replit` / `replit.nix` already request Python 3.11 and `ffmpeg-headless`; no existing
   packages are removed.
3. In the Shell:
   ```bash
   pip install -e .
   python scripts/download_model.py
   ```
   (`uv sync` also works.) Weights go to `.cache/models` inside the workspace, so they are
   downloaded once, not per request.
4. Add `GEMINI_API_KEY` (and optionally `APP_PASSWORD`, `WHISPER_MODEL`) in **Secrets**.
5. Press **Run**. Verify `GET /api/health` returns `"ok": true`.

### Resource measurements

Measured on a desktop CPU with `cpu_threads=2`, int8, 110 s Vietnamese speech:

| Model | Transcription time | Peak RAM (Whisper process) | Quality on the test clip |
|---|---|---|---|
| `tiny` | 15 s | ~270 MB | many word errors |
| `base` | 23 s | ~340 MB | understandable; some errors, e.g. "25 phút" heard as "205 phút" |
| `small` | 56 s | ~710 MB | clearly better; numbers correct |

Add roughly 100–150 MB for FastAPI, python-pptx and image handling. A shared 0.5 vCPU on
Replit will be several times slower than these numbers (not measured yet) — expect minutes,
not seconds, for a 10-minute video with `base`.

### Choosing a Replit runtime

| Option | Fits this app? | Notes |
|---|---|---|
| **Workspace (Run button)** | Good for development and demos | Free, but stops when idle/closed; workspace RAM on the free plan may be too small for `base` (use `WHISPER_MODEL=tiny`) |
| **Autoscale deployment** | Not recommended | Scales to zero and can stop instances between requests; long background jobs and the on-disk job state can be lost; cold starts reload the Whisper model |
| **Reserved VM deployment** | Recommended for real use | Always-on process keeps the model in memory and background jobs alive; pick ≥ 2 GB RAM for `base`/`small`. Paid monthly — check current pricing in Replit before choosing |

Deployments are **not** configured in `.replit`; choose and pay for one deliberately via
Replit's Publishing tool. Deployment file systems are not persistent across redeploys, which
is fine because jobs are temporary (24 h) — but the Whisper model must be downloaded again
after a redeploy (run `scripts/download_model.py` in the build command).

## Limitations

* MVP limits: MP4 only, ≤ 80 MB, ≤ 10 minutes, ≤ 8 frames (all configurable).
* One job runs at a time; others wait (queue max `MAX_QUEUE_SIZE`).
* The job queue is in-process: if the server restarts mid-job, that job is marked failed and
  can be retried (cached transcript is reused). It is not a durable queue.
* Text-fit uses conservative glyph-width estimates rather than real font metrics, so some
  boxes may end up with more whitespace than strictly necessary.
* Speech recognition quality depends on the model size and audio; proper names are often wrong.
* Without `GEMINI_API_KEY` the offline outline only splits and extracts transcript sentences —
  it is a fallback, not a substitute for the AI outline.
* No external image search or AI image generation; visuals are video frames and native shapes.
* URL import: automatic retrieval is **not verified live** for any platform. Only metadata
  (YouTube oEmbed) was tested against the real service; downloads were tested with mocks and a local
  file. Facebook Graph retrieval is untested without a real token and Page video.
* yt-dlp on YouTube from a datacenter IP (Replit) is frequently blocked with a bot check, and recent
  yt-dlp versions need a JavaScript runtime (e.g. Deno) for YouTube. The app reports this as
  "bot check" and does not work around it. Keep yt-dlp updated (`pip install -U yt-dlp`) if used.
* The URL endpoints are unauthenticated and unlimited by default (owner's choice). On a public
  Repl anyone can make the server fetch metadata; set `URL_RATE_LIMIT_PER_MIN` and/or `APP_PASSWORD`
  if that becomes a problem.
* Downloads run in-process; a restart during download marks the job failed (press **Thử lại**).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/api/health` shows `ffmpeg: null` | Make sure `replit.nix` / `.replit` includes `ffmpeg-headless` and reload the Repl; locally install FFmpeg and add it to `PATH` |
| "Không đủ bộ nhớ…" / process killed during transcription | Set `WHISPER_MODEL=tiny` or use a machine with more RAM |
| "GEMINI_API_KEY không hợp lệ…" | Recreate the key in AI Studio and update the Secret |
| "Model Gemini … không tồn tại" | Set `GEMINI_MODEL` to a model listed in AI Studio for your key |
| HTTP 429 from Gemini after retries | Free-tier quota reached; wait or reduce usage, then press **Thử lại** |
| Job failed after a restart | Press **Thử lại**; cached steps are reused |
| URL shows "Tự động tải … đang tắt" | Expected default. Upload the MP4 instead, or set `YOUTUBE_RETRIEVAL` / `FACEBOOK_RETRIEVAL` only if you have permission |
| Facebook URL: "chưa cấu hình FACEBOOK_ACCESS_TOKEN" | Add a Page access token to `.env` (local) or Replit Secrets, then restart |
| Facebook URL: "token … không hợp lệ hoặc đã hết hạn" | Generate a new (long-lived) Page token |
| Facebook URL: "Token Facebook hiện tại không đọc được video này" | The video is not on the Page the token belongs to. Run `python scripts/check_facebook_token.py "<video URL>"` to see token type, permissions and managed Pages |
| URL: "yêu cầu xác minh chống bot" | The platform blocks the server IP; use MP4 upload |
