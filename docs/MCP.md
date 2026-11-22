# Worker + MCP (thin gateway)

The Cloudflare Worker (`worker/`) is the public surface. The Python container remains the brain (`toolreg.execute()`).

## Routes

| Path | Handler |
|------|---------|
| `/` static assets | Workers Assets (`static/` + `fonts/`) |
| `/api/*` | Proxied to FastAPI container |
| `https://ttcaptiontool.com/mcp` | Official MCP Streamable HTTP |
| `/api/spark/callback` | Worker verifies secret → `/internal/spark/ingress` |
| Cron every 6h | `/internal/cleanup` |

## Secrets

Set with `wrangler secret put` from `worker/`:

- `INTERNAL_API_KEY` — Worker → container shared secret
- `MCP_DEV_KEY` / `MCP_DEV_UID` — local trusted MCP key (optional)
- `DUOPLUS_API_KEY`, `SCRAPE_CREATORS_API_KEY`, `SPARK_CALLBACK_*`
- Existing Firebase / R2 / LLM / Meta secrets

## Connect Claude / OpenClaw

1. Open Settings → **MCP** → Create trusted key (copy once).
2. Cursor / Claude: connector URL `https://ttcaptiontool.com/mcp` → Authorization Bearer `ctk_…`
3. OpenClaw: `mcp.servers.caption-tool.url` = `https://ttcaptiontool.com/mcp` + `transport: streamable-http` + Bearer header

Regenerate tool schemas after changing Python tools:

```bash
python scripts/generate_mcp_tool_defs.py
```

Smoke internal plumbing:

```bash
ALLOW_DEV_NO_AUTH=1 uvicorn app.main:app --port 8765
python scripts/e2e_mcp_smoke.py --base http://127.0.0.1:8765
```
