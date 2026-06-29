# clause_finder_mcp (HTTP / remote connector version)

This is the **real, URL-addable** version of clause_finder_mcp — a
streamable-HTTP MCP server you deploy once, then anyone (including you,
from claude.ai web, no local install) can add via **Settings → Connectors
→ Add custom connector**, just by pasting the URL.

## What changed from the local (stdio) version

| | Local version | This version |
|---|---|---|
| Transport | stdio (subprocess) | streamable HTTP |
| Runs on | the user's own machine | a server you host |
| Document input | local file path | `document_url` (public URL) or `document_text` (raw text) |
| Install | edit Claude Desktop config, restart app | paste a URL into Connectors settings |
| Works on claude.ai web | No | Yes |

The **tool logic itself didn't change** — same search, same section
detection, same error handling discipline. Only the transport
(`mcp.run(transport="streamable-http")` instead of `mcp.run()`) and the
input shape changed, because a remote server can't read a path on your
laptop.

## Why document_url / document_text instead of a path

A server running on Vercel/Render/wherever has its own filesystem, not
yours. It physically cannot open `/Users/you/Documents/contract.pdf` —
that path means nothing on the remote machine. So this version accepts
either:
- `document_url` — a public link to a PDF/DOCX/TXT, which the server
  downloads itself, or
- `document_text` — raw text, for when the calling agent already has the
  content (e.g. it read an uploaded file in the conversation and wants
  this tool to just search within that text)

Exactly one of the two must be provided — the input model enforces this
and rejects calls that give both or neither, with a clear error.

## Run it locally first (to test before deploying)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 server.py
```

This starts a real HTTP server on `http://127.0.0.1:8000/mcp`. You can
sanity-check it's alive with:

```bash
curl -i -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
```
A `200 OK` with a `serverInfo` block back means it's working.

## Deploying it so it has a real public URL

Pick any host that can run a long-lived Python process and exposes a
port over HTTPS. Three common options, roughly in order of simplicity
for a small server like this:

### Option 1: Render / Railway (simplest — no config files needed)
1. Push this folder to a GitHub repo.
2. Create a new "Web Service" on Render or Railway, point it at the repo.
3. Set the start command: `python3 server.py`
4. Set the port to `8000` (or read `PORT` from env — see note below).
5. Deploy. You get a URL like `https://clause-finder.onrender.com`.
6. Your connector URL is that, plus `/mcp` — e.g.
   `https://clause-finder.onrender.com/mcp`.

### Option 2: A VPS you already control
1. Copy this folder to the server.
2. `pip install -r requirements.txt` (ideally in a venv).
3. Run it behind a process manager (e.g. `systemd` or `pm2`) so it
   restarts on crash/reboot, and put it behind a reverse proxy
   (Caddy/nginx) for HTTPS — MCP connectors must be served over HTTPS to
   be added by claude.ai.
4. Your connector URL is `https://your-domain.com/mcp`.

### Option 3: Cloudflare Workers / Vercel (serverless)
These require adapting the transport to their request/response model
rather than running a persistent `uvicorn` process — more setup than
Options 1–2. Worth doing once you want this production-grade and
auto-scaling, not for a first test.

### Important: respect the PORT environment variable
Most hosts (Render, Railway, etc.) assign a port dynamically via a
`PORT` env var rather than letting you hardcode 8000. Update the bottom
of `server.py` to:
```python
import os
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    mcp.run(transport="streamable-http", port=port)
```

## Adding it to Claude once deployed

1. Go to claude.ai → **Settings → Connectors**
2. Click **Add custom connector**
3. Paste your URL, e.g. `https://clause-finder.onrender.com/mcp`
4. Since this version has no auth yet, just confirm — no API key or
   OAuth step needed
5. The two tools (`clause_finder_search`, `clause_finder_list_sections`)
   are now available in any conversation, from any device, no local
   install at all

## About "no auth yet"

This version is deliberately open — anyone with the URL can call it.
That's fine for testing or sharing with people you trust, but for real
public distribution you'd want at minimum an API key check on incoming
requests (reject calls missing a correct header) before sharing the URL
widely. That's a deliberately separate next step, not part of this pass.

## Files

| File | Purpose |
|---|---|
| `server.py` | The MCP server, streamable-HTTP transport, two tools |
| `requirements.txt` | Dependencies |
