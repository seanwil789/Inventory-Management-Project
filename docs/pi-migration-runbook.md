# Pi Migration Runbook

**Goal:** move the Django app + invoice pipeline + cron schedule from the Chromebook (`penguin`, 100.65.60.63) to a Raspberry Pi at **Sean's home**, so the kitchen display + cron run 24/7 instead of going down whenever the Chromebook lid closes.

**Scope:** lift-and-shift. Same Django, same SQLite, same cron schedule. SQLite stays — no Postgres migration in this round. Postgres is a Phase-2 concern when we add a second user (Albert) and want concurrent writes.

**Estimated wall time once hardware is in hand:** 3–4 hrs. Most of it is OS imaging + first-boot waits, not actual migration.

## Why Pi-at-home, not Wentworth basement

Decision made 2026-04-25 as part of the IP-defensive posture (see `project_ip_ownership.md`, `project_ip_case_strength_analysis.md`). Locating the Pi at Sean's home — combined with out-of-pocket hardware purchase + the no-work-during-hours rule — establishes "personal hardware on personal network running on personal time" as the going-forward fact pattern. Wentworth basement install would have left the Pi on Wentworth power and Wentworth network, weakening the personal-asset story.

Operational consequence: the Apolosign at Wentworth back-of-house reaches the Pi over Tailscale. KCC ↔ Pi-at-home routing is fully tunnel-based. Home internet stability is now load-bearing for the kitchen display.

---

## 1. Hardware (already ordered out-of-pocket 2026-04-25)

Purchased personally per the IP-defensive posture. Receipts to be archived in personal Gmail (`kitchen-project-receipts` folder) per `project_ip_evidence_archive.md`.

| Part | Why |
|---|---|
| **Raspberry Pi 4 Model B, 8GB** | 4GB is enough for current workload but 8GB future-proofs against running both runserver + a Postgres later. |
| **Argon ONE M.2 V2 case** (M.2 SATA variant for Pi 4) | Sealed aluminum body acts as the heatsink — passive cooling, no fan. M.2 SATA SSD bay underneath: no SD card to fail under sustained writes. |
| **240GB M.2 SATA SSD** (Crucial MX500 or similar) | Boots OS + holds db.sqlite3 + .ocr_cache. M.2 *SATA* (not NVMe) — Pi 4 can't do NVMe; the Argon case has a SATA-to-USB3 bridge. |
| **32GB microSD** | One-time use for initial OS imaging, before SSD-clone. |
| **Official Raspberry Pi 15.3W USB-C PSU** | Critical: random USB-C bricks cause undervoltage faults that look like software bugs. |

**Skipped vs. the original Wentworth-basement plan:**
- ~~APC Back-UPS 425VA~~ — basement-power-flicker hedge. Less relevant at home unless Sean's home power is unstable.
- ~~Wall/shelf mounting bracket for off-floor placement~~ — basement dust/spill hedge. Less relevant at home.
- ~~Silica gel pack for basement humidity~~ — not a home concern.

**Apolosign 21.5"** — kitchen display thin client. Arrives 2026-04-26. Lives at Wentworth back-of-house (out of tour-visible sight per IP-defensive restrictions).

---

## 2. Pre-arrival prep

### 2a. Push the repo to GitHub + rotate the GHP token

The Pi will clone fresh from GitHub. **Rotate the GHP token in `.git/config` before pushing** — currently embedded with full repo access (`ghp_k6MD…`). This was flagged in the 2026-04-24 snapshot as required-before-Pi-push and remains unrotated as of 2026-04-25.

```bash
# From Chromebook, before migration day:
cd /home/seanwil789/my-saas
git status                 # confirm clean
git push origin main       # may fail until token rotated
```

**Token rotation steps (do these BEFORE the push):**
1. GitHub → Settings → Developer settings → Personal access tokens → Generate new token (classic or fine-grained, your call). Scope: `repo`. Expiry: 90 days.
2. Copy the new token immediately (one-time display).
3. On the Chromebook: `gh auth login` (recommended), or edit `.git/config` directly to replace the embedded token. `gh auth login` is cleaner — stores the token in your OS keychain rather than in cleartext inside `.git/config`.
4. Revoke the old token in GitHub Settings.

After rotation, `git push origin main` succeeds.

### 2b. Generate a Tailscale auth key

Sean's personal Tailscale account already exists (confirmed 2026-04-25). In the admin console (`https://login.tailscale.com/admin/settings/keys`):
- Create a **reusable, non-ephemeral** auth key
- Set expiry to 90 days
- Tag: `tag:server`
- Copy the `tskey-auth-...` value — you'll paste it into the Pi during step 6

### 2c. Pre-flight WiFi check at the home install spot

Verify your planned Pi location at home has workable signal:

1. Stand at the planned install location with your phone connected to home WiFi.
2. Run a speed test (fast.com or speedtest.net). Target: **≥10 Mbps down, ≥2 Mbps up**, latency under 100ms.
3. Stream a video for 60 seconds — no stutters = good enough.
4. If signal is marginal: move the Pi closer to the router OR add a mesh node OR use ethernet (Cat6 cable, ~$8).

Per Sean's read 2026-04-25, home WiFi is "ok, not great, not terrible." That's adequate for the Pi's outbound work (Drive polls, DocAI calls) and for serving the kitchen display over Tailscale, but plan for occasional brief drops. The Pi tolerates them (Tailscale auto-reconnects, hourly cron catches up next run); persistent <50% signal causes operational pain.

### 2d. Network-stability consideration unique to Pi-at-home

If your home internet is down, the Apolosign at Wentworth shows nothing — there's no local fallback. Mitigations to consider:
- Apolosign's Fully Kiosk Browser can cache the last-fetched page; configure to keep showing it on connection loss.
- A second hourly cron retry catches missed batch runs after connectivity restores.
- For long outages (>24 hr), accept that the kitchen display goes dark until reconnect. This is the trade for the cleaner IP fact pattern.

### 2e. Inventory current state

State to copy from Chromebook → Pi (verified 2026-04-25):
- `~/my-saas/.env` (10 environment variables)
- `~/my-saas/invoice_processor/credentials/service_account.json` (2.4 KB)
- `~/my-saas/db.sqlite3` (2.5 MB)
- `~/my-saas/.ocr_cache/` (18 MB, ~290+ cached entries)
- `~/my-saas/.invoice_totals/` (20 KB, 4 monthly JSON files)
- `~/my-saas/.historical_stats/` (24 KB, production tracker JSON)
- `~/my-saas/invoice_processor/mappings/` (244 KB, item_mappings + learned_rules + negative_matches)
- `~/my-saas/.kitchen_ops/` (7.2 MB, Word docs)

Total payload: ~30 MB. Trivial.

The transfer script `docs/pi-migration-rsync.sh` packages all of this into a single rsync over SSH. Chromebook will be at home tonight (Sean brings it home/work daily) — both devices co-located, transfer over local WiFi.

---

## 3. Hardware assembly (~30 min)

1. **Insert the M.2 SSD** into the Argon ONE M.2 V2 expansion board (single screw at the far end).
2. **Mount the Argon expansion onto the Pi** via the GPIO header — case instructions show alignment.
3. **Slot the Pi+expansion into the aluminum body**, screw down (4 screws on the underside).
4. **Don't insert the microSD yet** — first boot will be from microSD imaged in the next step.

Apply **thermal pad** between the SoC and the aluminum lid (Argon ONE includes one — peel and stick to the aluminum, not the SoC). The aluminum body becomes the heatsink. Skip this step and the Pi will throttle within minutes.

---

## 4. OS install — image microSD, first boot (~45 min)

### 4a. Image Raspberry Pi OS Lite (64-bit)

Use **Raspberry Pi Imager** on your Chromebook (Linux app) or any other machine:
- OS: **Raspberry Pi OS Lite (64-bit)** — Bookworm. *Lite*, not Desktop — no GUI needed.
- Storage: the 32GB microSD card.
- **Click "Edit settings" gear icon** before writing:
  - Hostname: `kitchen-pi` (was `wentworth-kitchen` in original draft — switched to a neutral name for IP-defensive cleanliness; Sean can override)
  - Enable SSH with public-key authentication
  - Username: `sean`
  - Paste your SSH public key (from `~/.ssh/id_ed25519.pub` on Chromebook — or generate one with `ssh-keygen -t ed25519`)
  - Locale: `America/New_York`
  - **Wifi: configure with your HOME SSID + password.** The Pi 4 has onboard 2.4 + 5 GHz dual-band — pick whichever band tested cleanest at the install spot in section 2c. (If you opted for ethernet instead, leave Wifi blank and plug in the cable on first boot.)

### 4b. First boot

1. Insert microSD into Pi, plug in USB-C power. (Plug in ethernet only if you opted out of WiFi in section 2c.)
2. Wait 90-120 seconds. The Pi boots, runs first-time config, joins your home WiFi network.
3. Find the Pi's local IP: log into your router admin page, look for `kitchen-pi`. Or scan: `nmap -sn 192.168.1.0/24` from another machine on the same WiFi.
4. SSH in: `ssh sean@<pi-ip>`. You're in.

**Troubleshooting if Pi doesn't appear on the network:** WiFi config typo is the common culprit. Re-image the SD card with corrected SSID/password (Raspberry Pi Imager re-flash is 5 min). If WiFi works at boot but drops persistently, fall back to ethernet — symptoms point to signal too weak for sustained operation.

### 4c. Update OS and install system packages

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y \
  python3.11 python3.11-venv python3-pip \
  poppler-utils libjpeg-dev libopenjp2-7 \
  sqlite3 git build-essential \
  rsync curl ca-certificates openssl \
  fonts-liberation
```

`poppler-utils` is required by `pdf2image`. `libjpeg-dev` and `libopenjp2-7` are required by Pillow / image processing inside DocAI parsing. `openssl` is needed for the backup script's encryption step.

Reboot once after the upgrade: `sudo reboot`.

### 4d. Set personal git identity on the Pi

Match the Chromebook's git identity (already personal — Sean Willcox <seanwil789@gmail.com>):

```bash
git config --global user.email "seanwil789@gmail.com"
git config --global user.name "Sean Willcox"
```

Verifies as part of the IP paper trail — every commit from any host is signed with personal identity.

---

## 5. Boot from SSD instead of microSD (~30 min)

This is where the dust-resilience payoff happens. SD cards die under sustained writes (Django logs, OCR cache writes, sqlite WAL). M.2 SSDs don't.

```bash
sudo apt install -y rpi-clone

# The SSD shows up as /dev/sda when the Argon M.2 board is mounted.
lsblk
# You should see two devices: mmcblk0 (the microSD) and sda (the SSD)

# Clone the running system to the SSD.
sudo rpi-clone sda
# Confirm the wipe when prompted. Takes ~5-10 min.

# Switch boot order to SSD-first:
sudo raspi-config
# → Advanced Options → Boot Order → USB Boot → OK
```

Power off, remove the microSD card, power back on. The Pi boots from the SSD. Verify with `df -h` — root should now be `/dev/sda1`, not `/dev/mmcblk0p1`.

**Keep the microSD aside** as a recovery boot disk. If the SSD ever fails, slot the SD back in and the Pi boots into a clean state.

---

## 6. Install Tailscale (~5 min)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --auth-key=tskey-auth-XXXXXXXXX --hostname=kitchen-pi
```

Use the auth key you generated in step 2b.

Verify the new node appears in your Tailscale admin console. Note the new tailnet IP (it'll be something like `100.x.y.z`). Save it — you'll need it for `ALLOWED_HOSTS` and for pointing the Apolosign at the new host.

**This is the load-bearing step for KCC reachability.** The Apolosign at Wentworth must reach Pi-at-home over Tailscale. Confirm Tailscale shows the node as connected before moving on.

---

## 7. Code + state transfer (~20 min)

### 7a. Clone the repo

```bash
ssh sean@kitchen-pi   # or its tailnet IP
cd ~
git clone https://github.com/seanwil789/Inventory-Management-Project.git my-saas
cd my-saas
```

If the GHP token in `.git/config` was rotated in step 2a, this clone uses HTTPS + your refreshed credentials. If you'd rather not embed creds in the URL, use SSH + an SSH deploy key on the Pi.

### 7b. Create venv + install Python deps

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt
```

Expect ~3–5 min for the first `pip install` — `google-cloud-documentai`, `pymupdf`, `rapidfuzz` all have arm64 wheels available so no compilation should be needed.

### 7c. Copy state from Chromebook to Pi

From your **Chromebook** (which is at home tonight per the 04-25 setup), run the helper script:

```bash
cd /home/seanwil789/my-saas
bash docs/pi-migration-rsync.sh sean@kitchen-pi
```

This rsyncs `.env`, `service_account.json`, `db.sqlite3`, `.ocr_cache/`, `.invoice_totals/`, `.historical_stats/`, `invoice_processor/mappings/`, and `.kitchen_ops/` to the Pi. Both hosts on the same home WiFi = transfer takes ~30s.

### 7d. Update `ALLOWED_HOSTS`

The new Pi has a different tailnet IP and hostname. Set in `.env`:

```
ALLOWED_HOSTS=localhost,127.0.0.1,kitchen-pi,100.x.y.z
```

(Replace `100.x.y.z` with the Pi's tailnet IP from step 6.)

### 7e. Run migrations + sanity check

```bash
cd ~/my-saas
source .venv/bin/activate
python manage.py migrate           # should say "No migrations to apply" — DB came over with state intact
python manage.py test myapp        # should pass 399 tests in ~60s
```

If `manage.py test` passes, the application code + DB schema + state are all clean on the new host. **This is the gate.** Don't proceed to step 8 until tests pass.

---

## 8. Service setup (~30 min)

We're going to run Django via systemd (not runserver from a terminal that closes when SSH disconnects).

### 8a. Install systemd unit

Copy from `docs/pi-systemd/django.service` to `/etc/systemd/system/` and enable:

```bash
sudo cp ~/my-saas/docs/pi-systemd/django.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now django.service
sudo systemctl status django.service     # confirm "active (running)"
```

The unit uses `runserver` for now (matches the Chromebook setup; same accessibility profile). When you're ready to go to gunicorn, swap the `ExecStart` line — gunicorn config also pre-staged in `docs/pi-systemd/gunicorn.conf.py`.

### 8b. Install cron

```bash
crontab -e
```

Paste the following (paths already adjusted for `/home/sean/`):

```
0 * * * * /home/sean/my-saas/run_invoice_batch.sh
@reboot sleep 60 && /home/sean/my-saas/run_invoice_batch.sh
0 8 * * 5 /home/sean/my-saas/run_budget_sync.sh
0 6 * * * /home/sean/my-saas/run_refresh_invoice_totals.sh
0 */6 * * * /home/sean/my-saas/run_mapping_review_apply.sh
0 7 * * * /home/sean/my-saas/run_mapping_review_discover.sh
5 0 1 * * /home/sean/my-saas/run_monthly_synergy_tab.sh
```

Verify each shell script's shebang and venv path resolves correctly on the new host:

```bash
grep -l 'home/seanwil789' /home/sean/my-saas/*.sh
```

If anything still references `/home/seanwil789/`, edit those scripts to use `/home/sean/`.

### 8c. Wire the off-site backup (post-install, but before declaring done)

The backup script `scripts/backup.sh` was written + tested 2026-04-25. To put it in production on the Pi:

```bash
# Set the encryption passphrase as an env var (or pull from a keyfile)
# Sean's choice on storage — 1Password / printed in safe / etc.
export KITCHEN_BACKUP_PASS="<your-chosen-passphrase>"

# Test once
bash ~/my-saas/scripts/backup.sh

# Add to crontab (runs nightly at 02:30)
echo '30 2 * * * KITCHEN_BACKUP_PASS=<your-passphrase> /home/sean/my-saas/scripts/backup.sh >> /home/sean/my-saas/logs/backup.log 2>&1' | crontab -e
```

**Off-site upload is pending Sean's destination decision** (Backblaze B2 recommended). Until configured, backups are local-only on the Pi — that's better than no backup but doesn't survive home-disaster scenarios. See `scripts/backup.sh` UPLOAD section for the configuration template.

---

## 9. Point the kitchen display + Albert's HP at the new host

### 9a. Apolosign

In Fully Kiosk Browser settings on the Apolosign:
- Start URL: `http://kitchen-pi:8000/display/` if Apolosign is on the tailnet, OR `http://100.x.y.z:8000/display/` (tailnet IP — name resolution can flake)
- **Note:** the Apolosign needs to be on Sean's tailnet to reach the Pi-at-home. Install Tailscale on the Apolosign as part of the 4-26 install. Without Tailscale, the Wentworth-network Apolosign cannot reach the home-network Pi.

### 9b. Albert's HP

Bookmark the new URL in his browser. Will need Tailscale if he's on the Wentworth network. (His machine was already on the tailnet per the original snapshot — verify still connected.)

### 9c. Sean's S24 FE

Same — bookmark the new URL. Already on the tailnet.

---

## 10. Verification (do all of these before declaring success)

```bash
# On the Pi
systemctl status django.service        # active (running)
crontab -l                              # 8 entries shown (7 cron + 1 backup)
curl -s http://localhost:8000/         # 302 redirect to /accounts/login/ (auth working)
curl -s http://localhost:8000/display/ # 200 OK (no auth required for display)

# On the Chromebook (or any tailnet device)
curl -s http://kitchen-pi:8000/display/ | head -20   # confirm tailnet routing

# Check first cron run
ls -la ~/my-saas/logs/                  # invoice_batch_*.log appears at next hourly mark
```

Wait until at least one full hourly cron cycle has fired (at the top of the next hour). Inspect the log — should match the same shape as the logs on Chromebook.

**Verify backup.sh fired in test mode** before relying on the cron entry:

```bash
KITCHEN_BACKUP_PASS=test bash ~/my-saas/scripts/backup.sh
ls -la ~/.kitchen-backups/    # should show today's encrypted tarball
```

---

## 11. Rollback plan

If anything goes sideways, the Chromebook is still functional and ready to take the load back:

1. Stop the Pi's services: `sudo systemctl stop django.service && crontab -r`.
2. On the Chromebook, restart the dev server (`python manage.py runserver 0.0.0.0:8000`) and re-enable the existing cron.
3. Point the Apolosign back at `http://penguin:8000/display/`.

Your data on the Pi vs. the Chromebook will diverge from the moment the Pi takes over. To re-sync after rollback, rsync the Pi's state back: `rsync -av --update sean@kitchen-pi:~/my-saas/{.ocr_cache,.invoice_totals,db.sqlite3} ~/my-saas/`. Use `--update` so the newer files win.

---

## 12. Ongoing operational notes

### Daily backup

Already covered in section 8c. Backup script handles it.

### Health monitoring

```bash
# CPU temp — should sit < 65°C with the aluminum case under sustained load
vcgencmd measure_temp

# SSD wear — check periodically
sudo apt install -y smartmontools
sudo smartctl -a /dev/sda | grep -i "power_on_hours\|wear"
```

If temps regularly exceed 75°C, the thermal pad isn't seated right — re-seat.

### Decommissioning the Chromebook

Once the Pi has been stable for 1–2 weeks of cron + display, the Chromebook can be retired from the tailnet. **Don't rush this** — the Pi backup is the rollback path. Also: since the Chromebook is Sean's personal device that travels home/work daily, "retire" means stop running the project on it, not physically dispose.

### Home network considerations (Pi-at-home specific)

- If you change ISPs / routers, the Pi's local IP may change. Use the Tailscale IP (`100.x.y.z`) in URLs whenever stable IP matters; it's persistent across home-network changes.
- If your home internet drops for > 24 hours, the kitchen display at Wentworth goes dark. Apolosign will show its last cached page if Fully Kiosk Browser is configured for that.
- Cron jobs that depend on Drive/Sheets/DocAI need outbound internet. They'll backlog gracefully and process on next hourly tick after restoration.

---

## 13. Post-migration TODOs (medium-term)

- **Postgres migration** when Albert starts using the system simultaneously with you. SQLite handles single-writer fine; concurrent writes are a sharp edge. ~2 hr migration when needed.
- **Gunicorn instead of runserver.** `runserver` works but is officially "for development". Pre-staged config at `docs/pi-systemd/gunicorn.conf.py`. ~15 min swap.
- **HTTPS / proper auth** if anything on the network is ever exposed beyond Tailscale. Currently fine because Tailscale is the only ingress, but production-grade deployment wants TLS + a real auth flow.
- **WhiteNoise wiring** — `whitenoise==6.12.0` is in requirements.txt but NOT in INSTALLED_APPS / MIDDLEWARE, and no STATIC_ROOT is set. `collectstatic` will fail until wired. Plan: do this BEFORE step 7e (test pass) so the Pi has a clean static-files setup. ~10 min.
- **Off-site backup destination configured** — see `scripts/backup.sh` UPLOAD section. Backblaze B2 recommended (~$0.005/GB/mo, 10GB free).
- **Watchdog** — if the Pi reboots unexpectedly, systemd brings Django back. The cron `@reboot sleep 60` catches up the invoice batch. But there's no alert if cron stops firing. A simple healthcheck → Tailscale-shared webhook → Sean's phone is a one-hour add when worth it.

---

## 14. IP-strategy crosswalk

This deployment is part of the broader 2026-04-25 IP-defensive posture. Decisions made here intentionally:

| Decision | IP rationale |
|---|---|
| Pi at home, not Wentworth basement | Personal hardware on personal network = "company resources" prong of invention-assignment claim further weakened |
| Out-of-pocket purchase | Personal financial investment paper trail |
| Hostname `kitchen-pi` (vs `wentworth-kitchen`) | Slightly less Wentworth-coupled in any future production-of-evidence scenario |
| KCC at Wentworth back-of-house | Tour-visibility restriction (per `project_ip_ownership.md`) |
| Tailscale for KCC ↔ Pi reachability | Personal tailnet, personal account = continued personal infrastructure |
| Backup encrypted with personal passphrase | Personal data control |

If any of these change later, update the IP memory accordingly so the case-strength analysis stays current.

---

## Appendix — what was inventoried before migration

Captured 2026-04-25 from `penguin`:

**Environment variables (10):** `GOOGLE_CREDENTIALS_PATH`, `SPREADSHEET_ID`, `MAPPING_TAB`, `DRIVE_ROOT_FOLDER_ID`, `DRIVE_INBOX_FOLDER_ID`, `DOCAI_PROJECT_ID`, `DOCAI_LOCATION`, `DOCAI_PROCESSOR_ID`, `SECRET_KEY`, `DEBUG`.

**Credentials files (1):** `invoice_processor/credentials/service_account.json` (~2 KB).

**State payload (~30 MB):** `db.sqlite3` (2.5 MB), `.ocr_cache/` (18 MB / 290+ entries), `.invoice_totals/` (20 KB / 4 files), `.historical_stats/` (24 KB), `.kitchen_ops/` (7.2 MB), `invoice_processor/mappings/` (244 KB).

**Codebase scale:** 149 commits, 31,672 LOC across the Python tree, 399 tests passing in ~60s, 27 migrations applied.

**Cron entries (current Chromebook):** 7 scheduled (hourly batch + on-reboot batch + weekly budget_sync + daily refresh_totals + 6-hourly mapping_apply + daily mapping_discover + monthly synergy_tab).

**Tailscale tailnet:** `penguin` (Linux, current Chromebook host), `alberts-hp` (Windows), `seans-s24-fe` (Android). Pi will join as `kitchen-pi`. Apolosign joins as part of 04-26 install.

**GCP project ownership:** Sean's personal account (confirmed 2026-04-25). All compute infrastructure — service account, DocAI processor, Vision API, billing — under Sean's personal billing.

**Hardware ownership:** Chromebook personal (travels home/work daily). Pi + Apolosign out-of-pocket 2026-04-25.
