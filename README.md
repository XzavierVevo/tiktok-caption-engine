# TikTok Caption Engine

**Bulk short-form ad production: N videos × M captions → N·M ready-to-post TikTok creatives, driven by an AI agent.**

Paste a list of hooks, drop in videos (upload or TikTok / YouTube / Pinterest links), and it renders every video × every caption in TikTok's native caption style: 2 videos × 5 captions = 10 creatives. A campaign chat agent writes and picks the hooks, and an MCP gateway lets Claude (or any MCP client) run the whole thing as tools: "make 40 variants of this hook for these 8 clips" is one message.

It's a multi-user web app: Google sign-in, per-user storage, a queue, and a Cloudflare edge in front of a GPU-or-CPU render container.

> **Public snapshot.** This repo is the caption, agent and MCP core. The account and posting automation for the production app stays private. Demo available on request.

## Why it matters for an ad team

Creative volume is the bottleneck in paid social. This turns one editor-day of caption work into a batch job:

- **Matrix mode**: every video × every caption, or **pair mode** 1:1, up to 200 jobs per batch
- **Skips finished combos**, so re-running a batch only renders what's new
- **Hook agent** (`app/agent/`): a tool-using LLM loop that drafts hooks, scores them and picks captions per clip, with Perplexity research
- **MCP server** (`worker/src/mcp/`): the same tools exposed to Claude / OpenClaw over Streamable HTTP with per-user API keys
- **Locked brand style** with live preview: TikTok Sans 500, white fill, 13.5% stroke, auto shrink-to-fit, color emoji
- **NVENC** when a GPU is present, libx264 in the cloud container

## How it works

```
Browser ──HTTPS──▶ Cloudflare Worker ──proxy──▶ Container (FastAPI + FFmpeg + Playwright)
   │                    │                           │
   ├─ Google sign-in    ├─ /api/*  → container      ├─ verifies Firebase ID tokens
   ├─ Bearer on /api    ├─ /mcp    Streamable HTTP  ├─ R2: every user's media under users/{uid}/
   └─ MCP key ctk_…     ├─ quotas, edge cache       ├─ job runtime + render queue
                        └─ D1 state, orchestration  └─ agent loop (Claude / OpenAI / Perplexity)
```

| Path | What it is |
|---|---|
| `app/encode.py`, `app/layout.py`, `app/caption_markers.py` | FFmpeg caption burn, safe-area layout, multi-line fitting |
| `app/jobs.py`, `app/job_runtime.py` | Batch matrix, queue, skip-already-rendered |
| `app/agent/` | Campaign chat agent: tool loop, providers, caption picking |
| `app/hooks.py`, `app/carousel_formats.py` | Hook generation and photo-carousel formats |
| `app/ingest.py`, `app/pinterest.py`, `app/library.py` | Link ingest from TikTok / YouTube / Pinterest |
| `app/tiktok_marketing/` | TikTok Marketing API (OAuth, creatives, reports, pixel) |
| `worker/` | Cloudflare Worker: auth, MCP server, quotas, orchestration, container proxy |
| `static/` | Single-page app (create, style, outputs, chat) |

## Stack

`python` `fastapi` `ffmpeg` `playwright` `typescript` `cloudflare-workers` `containers` `d1` `r2` `firebase-auth` `mcp` `claude` `openai`

## Run it locally

```powershell
pip install -r requirements.txt
playwright install chromium
copy .env.example .env      # fill in what you need; dev mode needs nothing
$env:ALLOW_DEV_NO_AUTH="1"
python -m uvicorn app.main:app --port 8765
```

## Secrets

Nothing sensitive is committed. Local keys go in `.env` (git-ignored, see `.env.example`). Production keys are Cloudflare Worker secrets (`wrangler secret put …`). User media is isolated per Firebase `uid`.
