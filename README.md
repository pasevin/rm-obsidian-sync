# rm-obsidian-sync

**Self-hosted bidirectional sync between reMarkable and Obsidian**

Handwritten notes on your reMarkable tablet are automatically transcribed (via LLM vision HWR) and written as Markdown into your Obsidian vault. Obsidian notes and PDFs are automatically uploaded to the tablet, mirroring your vault's folder structure.

Built on top of [rmfakecloud](https://github.com/ddvk/rmfakecloud) — a self-hosted reMarkable cloud backend.

---

## How it works

```
reMarkable tablet
      │  (wifi, proxied to your server)
      ▼
 rmfakecloud          ← self-hosted cloud backend (Docker)
      │
      ▼
rm-obsidian-sync      ← this daemon (systemd user service)
   │           │
   ▼           ▼
Obsidian    Obsidian
 vault  ←─  vault
(writes)   (watches)
```

### reMarkable → Obsidian

1. Poll loop detects new/modified notebooks every 60s via the sync15 blob root hash
2. Stroke data (`.rm` files) parsed via [`rmscene`](https://github.com/ricklupton/rmscene)
3. Each inked page rendered to PNG via a custom Cairo-based stroke renderer
4. Handwriting recognised via **LLM vision** (OpenRouter, free tier) → Tesseract fallback
5. Markdown note written to `<VAULT_RM_ROOT>/<Notebook Name>/` with embedded page images
6. **Deletions propagate** — if a notebook is deleted on the tablet, the corresponding vault directory is removed on the next poll cycle (see [Deletion behaviour](#deletion-behaviour))

### Obsidian → reMarkable

1. Vault watcher (5 s debounce) detects saved `.md` files
2. Markdown converted to PDF via `pandoc` → `wkhtmltopdf` with e-ink–optimised CSS
3. PDF uploaded to rmfakecloud under an `Obsidian/` root folder, mirroring vault structure
4. Tablet syncs automatically on next WiFi connection

---

## Requirements

- Linux server (VPS or home server) with Docker
- [rmfakecloud](https://github.com/ddvk/rmfakecloud) running (see setup below)
- reMarkable tablet with developer mode enabled and proxy configured
- Python 3.11+
- [`uv`](https://github.com/astral-sh/uv) (recommended) or pip
- `pandoc` + `wkhtmltopdf` (for Obsidian→reMarkable uploads)
- An [OpenRouter](https://openrouter.ai) API key (free tier works; used for HWR)
- Your Obsidian vault accessible on the server (local path, NFS, rclone mount, etc.)

---

## Setup

### 1. Run rmfakecloud

```bash
mkdir -p ~/rmfakecloud/data
cat > ~/rmfakecloud/docker-compose.yml << 'EOF'
services:
  fix-permissions:
    image: busybox
    volumes:
      - ./data:/data
    command: sh -c "while true; do chmod -R a+rX /data 2>/dev/null; sleep 30; done"
    restart: unless-stopped

  rmfakecloud:
    image: rmfakecloud-custom:latest   # see note below
    container_name: rmfakecloud
    restart: unless-stopped
    depends_on: [fix-permissions]
    ports:
      - "127.0.0.1:3000:3000"
    environment:
      - JWT_SECRET_KEY=change-me-random-32-chars
      - STORAGE_PATH=/data
    volumes:
      - ./data:/data
EOF
cd ~/rmfakecloud && docker compose up -d
```

> **Important — build a patched rmfakecloud image first:**
>
> The upstream image has a 3-hour JWT TTL hardcoded. When the container restarts, the tablet can't reconnect without a USB cable. Patch it:
>
> ```bash
> git clone --depth=1 https://github.com/ddvk/rmfakecloud rmfakecloud-src
> # Apply the patch from deploy/rmfakecloud.patch
> cd rmfakecloud-src && patch -p1 < ../rm-obsidian-sync/deploy/rmfakecloud.patch
> docker build -t rmfakecloud-custom:latest .
> ```
>
> The patch sets the JWT TTL to 30 days and adds a 30-day grace leeway on validation so the tablet auto-recovers after restarts.

### 2. Expose rmfakecloud publicly

The tablet needs to reach your server over HTTPS. Options:

- **Tailscale Funnel** (recommended, free):
  ```bash
  tailscale funnel 3000
  ```
- **Caddy** reverse proxy with a real domain + Let's Encrypt
- **Cloudflare Tunnel**

Note the public URL — you'll need it for the tablet proxy.

### 3. Configure the reMarkable tablet

Enable developer mode on the tablet (Settings → Help → Copyrights → tap the version number 5 times).

SSH into the tablet (USB cable required for initial setup):
```bash
ssh root@10.11.99.1
```

Configure the cloud proxy:
```bash
# Point the tablet at your rmfakecloud instance
cat > /etc/systemd/system/rmfakecloud-proxy.service << 'EOF'
[Unit]
Description=reMarkable cloud proxy
After=network.target

[Service]
ExecStart=/usr/bin/env PROXY_HOST=https://your-server.example.com \
  /usr/lib/remarkable/proxy-daemon
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl enable --now rmfakecloud-proxy.service
```

Register a new account on rmfakecloud (visit `https://your-server.example.com`), then tap **Settings → Account** on the tablet to sign in.

### 4. Install rm-obsidian-sync

```bash
git clone https://github.com/pasevin/rm-obsidian-sync
cd rm-obsidian-sync
cp .env.example .env
$EDITOR .env          # fill in your values
./install.sh
```

The install script:
- Creates a Python venv and installs the package
- Registers the daemon as an rmfakecloud device (prompts for a pairing code if needed)
- Installs and starts a systemd user service

### 5. Register the daemon with rmfakecloud

In the rmfakecloud web UI (`http://localhost:3000`), go to **Devices → Add device** and copy the one-time code. Then:

```bash
rm-register --code <your-code>
```

---

## Configuration

All settings live in `.env` (copy from `.env.example`):

| Variable | Required | Default | Description |
|---|---|---|---|
| `RMFAKECLOUD_URL` | ✓ | `http://localhost:3000` | Internal URL of rmfakecloud (not the public one) |
| `RMFAKECLOUD_USER` | ✓ | — | rmfakecloud account email |
| `RMFAKECLOUD_PASS` | ✓ | — | rmfakecloud account password |
| `RMFAKECLOUD_DATA_PATH` | ✓ | `~/rmfakecloud/data` | Absolute path to rmfakecloud data directory |
| `VAULT_PATH` | ✓ | `~/vault` | Absolute path to Obsidian vault root |
| `VAULT_RM_ROOT` | — | `Inbox/reMarkable` | Sub-path for reMarkable notes inside vault |
| `OPENROUTER_API_KEY` | ✓ | — | OpenRouter key for LLM vision HWR |
| `LLM_VISION_MODEL` | — | `nvidia/nemotron-nano-12b-vl:free` | OpenRouter model for HWR |
| `WEBHOOK_PORT` | — | `9090` | Daemon listen port |
| `PANDOC_BIN` | — | `pandoc` | Path to pandoc binary |
| `WKHTMLTOPDF_BIN` | — | `wkhtmltopdf` | Path to wkhtmltopdf binary |
| `WKHTMLTOPDF_LIB_PATH` | — | — | Extra `LD_LIBRARY_PATH` entry for wkhtmltopdf |

---

## Vault structure

### reMarkable → Obsidian

Notes land in:
```
<VAULT_PATH>/<VAULT_RM_ROOT>/<Notebook Name>/
├── <Notebook Name>.md     ← transcribed text + page image embeds
└── raw/
    ├── page-1.png          ← stroke renders (only inked pages)
    └── page-3.png
```

Example frontmatter:
```yaml
---
created: 2026-06-11
source: remarkable
notebook_id: bc470ed3-1464-429e-995f-624ccc35f8f1
synced_at: 2026-06-11T10:19:12Z
tags:
  - remarkable
---
```

### Obsidian → reMarkable

All vault notes appear on the tablet under a single `Obsidian/` root folder, with the vault directory hierarchy mirrored:

```
Obsidian/
├── Personal/
├── Projects/
│   └── My Project/
└── Work/
    └── OpenZeppelin/
        ├── Meetings/
        └── Projects/
```

To exclude a note from upload, add to its frontmatter:
```yaml
rm_exclude: true
```

Notes under `Inbox/reMarkable/` and `_templates/` are always excluded.

---

## Deletion behaviour

When you delete a notebook on the tablet, the poll loop detects the missing document on the next cycle and removes the corresponding vault directory. There are two cases:

**Note untouched in Obsidian since last sync → hard delete**
```
Delete on tablet → 60s poll → vault dir removed → state cleaned
```

**Note edited in Obsidian after last sync → safe backup**

If the `.md` file has a newer `mtime` than the last `synced_at` timestamp, the entire notebook directory is **moved** to a timestamped backup next to where it lived, rather than deleted outright:

```
Inbox/reMarkable/My Note/          ← original (has post-sync edits)
Inbox/reMarkable/My Note.deleted-20260611T113821/  ← backup
```

The backup directory does not appear in Obsidian's sidebar (it contains no `.md` file at the root level) but is fully recoverable from the filesystem.

---

## CLI reference

```bash
# Register daemon as rmfakecloud device
rm-register --code <one-time-code>

# Upload a specific file to the tablet
rm-upload path/to/note.md
rm-upload path/to/doc.pdf --folder "Reading List"

# Configure rmfakecloud webhook
rm-setup-webhook --url http://localhost:9090/webhook

# Manually trigger uploads
curl -X POST http://localhost:9090/upload/seed   # full vault re-upload
curl -X POST http://localhost:9090/upload/sync   # changed notes only
```

---

## Logs & monitoring

```bash
# Follow daemon logs
journalctl --user -u rm-obsidian-sync -f

# Service status
systemctl --user status rm-obsidian-sync

# Restart after config change
systemctl --user restart rm-obsidian-sync
```

---

## Troubleshooting

**Tablet shows cloud icon with a cross**

The user JWT expired. With the patched rmfakecloud build this self-heals. If you're using the upstream unpatched image: plug in USB, SSH to tablet, and clear the UserToken:
```bash
ssh root@10.11.99.1 "sed -i 's/^UserToken=.*/UserToken=/' \
  /home/root/.config/remarkable/xochitl.conf && systemctl restart xochitl"
```

**Notes not syncing tablet → Obsidian**

Check that `RMFAKECLOUD_DATA_PATH` points to the correct directory and that the blob files are world-readable (the `fix-permissions` sidecar handles this if using the provided docker-compose).

**PDF upload fails**

Ensure `pandoc` and `wkhtmltopdf` are installed and reachable. Set `PANDOC_BIN` / `WKHTMLTOPDF_BIN` in `.env` if they're in non-standard locations. For the statically-linked wkhtmltopdf build from wkhtmltopdf.org, also set `WKHTMLTOPDF_LIB_PATH`.

---

## Architecture

```
src/rm_sync/
├── main.py              — FastAPI app, lifespan, poll loop
├── config.py            — Typed config from environment
├── auth.py              — Device registration, JWT management
├── rm_client.py         — rmfakecloud REST API client (sync15 blob reader)
├── rm_parser.py         — ZIP/blob archive extraction
├── stroke_renderer.py   — .rm stroke data → PNG (Cairo)
├── hwr_client.py        — HWR chain: LLM vision → Tesseract fallback
├── vault_writer.py      — Markdown + frontmatter writer
├── obsidian_uploader.py — Markdown → PDF → rmfakecloud upload
├── vault_watcher.py     — watchfiles-based vault change detector
├── webhook.py           — FastAPI routes (/webhook, /upload/*)
└── cli.py               — Click CLI commands
```

---

## Credits

- [rmfakecloud](https://github.com/ddvk/rmfakecloud) by ddvk — the self-hosted reMarkable cloud
- [rmscene](https://github.com/ricklupton/rmscene) by Rick Lupton — reMarkable stroke format parser
- [OpenRouter](https://openrouter.ai) — LLM API gateway (free vision models for HWR)

---

## License

MIT
