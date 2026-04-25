# Pi Migration Runbook

**Goal:** move the Django app + invoice pipeline + cron schedule from the Chromebook (`penguin`, 100.65.60.63) to a wall-mounted Raspberry Pi in the Wentworth basement, so the kitchen display + cron run 24/7 instead of going down whenever the Chromebook lid closes.

**Scope:** lift-and-shift. Same Django, same SQLite, same cron schedule. SQLite stays — no Postgres migration in this round. Postgres is a Phase-2 concern when we add a second user (Albert) and want concurrent writes.

**Estimated wall time once hardware is in hand:** 3–4 hrs. Most of it is OS imaging + first-boot waits, not actual migration.

---

## 1. Hardware to order

### Recommended bundle (~$155 total)

| Part | Why | Approx. cost |
|---|---|---|
| **Raspberry Pi 4 Model B, 8GB** | 4GB is enough for current workload but 8GB future-proofs against running both runserver + a Postgres later. Stable platform, deep community support, well-tested in low-airflow enclosures. | ~$75 |
| **Argon ONE M.2 V2 case** (the M.2 SATA variant for Pi 4) | Sealed aluminum body acts as the heatsink — passive cooling, no fan to pull dust through. GPIO covered when assembled. M.2 SATA SSD bay underneath: **no SD card to fail**. The single biggest reliability win for unattended deployment. | ~$35 |
| **240GB M.2 SATA SSD** (Crucial MX500, Samsung 870 EVO, or WD Blue SA510) | Boots the OS + holds db.sqlite3 + .ocr_cache. M.2 *SATA* (not NVMe) — Pi 4 can't do NVMe; the Argon ONE M.2 V2 has a SATA-to-USB3 bridge built in. | ~$25 |
| **32GB microSD** (SanDisk Ultra or similar) | One-time use for initial OS imaging, before SSD-clone. | ~$8 |
| **Official Raspberry Pi 15.3W USB-C PSU** | Critical: random USB-C bricks cause undervoltage faults that look like software bugs. Use the official one. | ~$10 |
| ~~Cat6 ethernet cable~~ | ~~Hardwired beats wifi~~ — Sean opted for WiFi (see "Pre-flight WiFi check" in section 2d). Pi 4 has onboard 2.4 + 5 GHz dual-band. Skip unless WiFi signal at install spot is too weak. | ~$0 |

### Why not a Pi 5
Pi 5 is faster but pulls 25W under load (vs 6W for Pi 4), runs hotter, needs the bigger 27W PSU, and the Argon ONE V3 case for Pi 5 is newer / less battle-tested. For your workload (Django runserver + 7 cron jobs + Tailscale daemon + sqlite) the Pi 4 is genuinely sufficient.

### Why not a refurbished mini-PC instead
Honestly, a $100 refurbished Lenovo ThinkCentre Tiny (M710q or M910q) would be **more reliable** than a Pi for this exact use case — sealed case, internal SSD, x86, more RAM headroom. If you're open to it, that's the choice I'd make for a 12-month deployment. The Pi is the right call if you specifically want low-power (6W vs 15W idle), small footprint, and the option to hand it to Albert at his property without thinking about Windows licensing.

### Optional but recommended

- **APC Back-UPS 425VA** (~$60): basement power flickers will corrupt SQLite mid-write. A small UPS gives 5–10 min of runtime — enough for a clean shutdown via systemd's `nut-server` integration. Skip if you've never had a power blip at Wentworth.
- **Wall/shelf mounting bracket** so the Pi sits 2+ ft off the floor (out of dust line, away from spills).
- **Silica gel pack** taped to a rafter near the Pi if the basement runs humid in summer — replace every 3 months.

### Skip these (common but wrong picks)

- **Stock plastic Raspberry Pi case with fan** — fan = dust ingress. No.
- **POE HAT** — only useful if you're powering over ethernet. You said you have outlets; skip.
- **Raspberry Pi 3** — too underpowered for Django + Python OCR libs.

---

## 2. Pre-arrival prep (do this now, before Sunday)

### 2a. Push the repo to GitHub

The Pi will clone fresh from GitHub. Make sure the latest commits are pushed. **Rotate the GHP token in `.git/config` before pushing** — currently embedded with full repo access.

```bash
# From Chromebook, before migration day:
cd /home/seanwil789/my-saas
git status                 # confirm clean
git push origin main       # confirm reachable
```

If `git push` errors with auth issues, rotate the GHP token first via GitHub Settings → Developer settings → Personal access tokens, then update `~/.git-credentials` or use `gh auth login`.

### 2b. Generate a Tailscale auth key

In the Tailscale admin console (`https://login.tailscale.com/admin/settings/keys`):
- Create a **reusable, non-ephemeral** auth key
- Set expiry to 90 days
- Tag: `tag:server`
- Copy the `tskey-auth-...` value — you'll paste it into the Pi during step 5

### 2c. Pre-flight WiFi check at the install spot

Before committing to WiFi-only deployment, verify the basement install spot has workable signal:

1. Stand at the planned install location with your phone connected to the Wentworth WiFi.
2. Run a speed test (fast.com or speedtest.net). Target: **≥10 Mbps down, ≥2 Mbps up**, latency under 100ms.
3. Stream a video for 60 seconds — no stutters = good enough for the Pi.
4. If signal is marginal/dropping: add a mesh extender ($30-50) placed at the basement door, OR fall back to ethernet (Cat6 cable, ~$8).

The Pi will tolerate brief WiFi drops (Tailscale auto-reconnects, hourly cron catches up next run), but persistent <50% signal causes operational pain.

### 2d. Inventory current state (already done — see end of doc)

The state to copy is:
- `~/my-saas/.env` (10 environment variables)
- `~/my-saas/invoice_processor/credentials/service_account.json`
- `~/my-saas/db.sqlite3` (2.5 MB)
- `~/my-saas/.ocr_cache/` (18 MB, 288 cached entries)
- `~/my-saas/.invoice_totals/` (20 KB, 4 monthly JSON files)
- `~/my-saas/.historical_stats/` (24 KB, production tracker JSON)
- `~/my-saas/invoice_processor/mappings/` (244 KB, item_mappings + learned_rules + negative_matches)

Total payload: ~21 MB. Trivial.

The transfer script at `docs/pi-migration-rsync.sh` packages all of this into a single `tar.gz` for one-shot copy.

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
  - Hostname: `wentworth-kitchen`
  - Enable SSH with public-key authentication
  - Username: `sean`
  - Paste your SSH public key (from `~/.ssh/id_ed25519.pub` on Chromebook — or generate one)
  - Locale: `America/New_York`
  - **Wifi: configure with the Wentworth SSID + password** (verified workable in section 2c). The Pi 4 has onboard 2.4 + 5 GHz dual-band — pick the band that tested cleanest at the install spot. (If you opted for ethernet instead, leave Wifi blank and plug in the cable on first boot.)

### 4b. First boot

1. Insert microSD into Pi, plug in USB-C power. (Plug in ethernet only if you opted out of WiFi in section 2c.)
2. Wait 90-120 seconds. The Pi boots, runs first-time config, joins the WiFi network.
3. Find the Pi's local IP: log into your router admin page, look for `wentworth-kitchen`. Or scan: `nmap -sn 192.168.1.0/24` from another machine on the same WiFi.
4. SSH in: `ssh sean@<pi-ip>`. You're in.

**Troubleshooting if Pi doesn't appear on the network:** WiFi config typo is the common culprit. Re-image the SD card with corrected SSID/password (Raspberry Pi Imager re-flash is 5 min). If WiFi works at boot but drops persistently, fall back to ethernet — symptoms point to signal too weak for sustained operation.

### 4c. Update OS and install system packages

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y \
  python3.11 python3.11-venv python3-pip \
  poppler-utils libjpeg-dev libopenjp2-7 \
  sqlite3 git build-essential \
  rsync curl ca-certificates \
  fonts-liberation
```

`poppler-utils` is required by `pdf2image`. `libjpeg-dev` and `libopenjp2-7` are required by Pillow / image processing inside DocAI parsing.

Reboot once after the upgrade: `sudo reboot`.

---

## 5. Boot from SSD instead of microSD (~30 min)

This is where the dust-resilience payoff happens. SD cards die under sustained writes (Django logs, OCR cache writes, sqlite WAL). M.2 SSDs don't.

```bash
# Install rpi-clone
sudo apt install -y rpi-clone

# The SSD shows up as /dev/sda when the Argon M.2 board is mounted.
# Verify:
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
sudo tailscale up --auth-key=tskey-auth-XXXXXXXXX --hostname=wentworth-kitchen
```

Use the auth key you generated in step 2b.

Verify the new node appears in your Tailscale admin console. Note the new tailnet IP (it'll be something like `100.x.y.z`). Save it — you'll need it for `ALLOWED_HOSTS` and for pointing the Apolosign at the new host.

---

## 7. Code + state transfer (~20 min)

### 7a. Clone the repo

```bash
ssh sean@wentworth-kitchen   # or its tailnet IP
cd ~
git clone https://github.com/seanwil789/Inventory-Management-Project.git my-saas
cd my-saas
```

If the GHP token in `.git/config` was rotated in step 2a, this clone uses HTTPS + your refreshed credentials. If you'd rather not embed creds in the URL: use SSH + an SSH deploy key on the Pi.

### 7b. Create venv + install Python deps

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt
```

Expect ~3–5 min for the first `pip install` — `google-cloud-documentai`, `pymupdf`, `rapidfuzz` all have arm64 wheels available so no compilation should be needed.

### 7c. Copy state from Chromebook to Pi

From your **Chromebook**, run the helper script:

```bash
cd /home/seanwil789/my-saas
bash docs/pi-migration-rsync.sh sean@wentworth-kitchen
```

This rsyncs `.env`, `service_account.json`, `db.sqlite3`, `.ocr_cache/`, `.invoice_totals/`, `.historical_stats/`, and `invoice_processor/mappings/` to the Pi.

### 7d. Update `ALLOWED_HOSTS`

The new Pi has a different tailnet IP and hostname. Either:

- **Option A** — set in `.env`:
  ```
  ALLOWED_HOSTS=localhost,127.0.0.1,wentworth-kitchen,100.x.y.z
  ```
  (Replace `100.x.y.z` with the Pi's tailnet IP from step 6.)

- **Option B** — leave the default in `settings.py`; the existing default already includes `localhost,127.0.0.1,penguin,100.65.60.63`. You'll lose the ability to access via the new hostname/IP unless you either set `.env` or update the default in `settings.py`.

I recommend Option A.

### 7e. Run migrations + sanity check

```bash
cd ~/my-saas
source .venv/bin/activate
python manage.py migrate           # should say "No migrations to apply" — DB came over with state intact
python manage.py test myapp        # should pass 399 tests in ~80s
```

If `manage.py test` passes, the application code + DB schema + state are all clean on the new host. This is the gate.

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

---

## 9. Point the kitchen display + Albert's HP at the new host

### 9a. Apolosign

In Fully Kiosk Browser settings on the Apolosign:
- Start URL: `http://wentworth-kitchen:8000/display/`
- (Or `http://100.x.y.z:8000/display/` if name resolution flakes — IPs always work)

### 9b. Albert's HP

Just bookmark the new URL in his browser. Same as above.

### 9c. Sean's S24 FE

Same — bookmark the new URL.

---

## 10. Verification (do all of these before declaring success)

```bash
# On the Pi
systemctl status django.service        # active (running)
crontab -l                              # 7 entries shown
curl -s http://localhost:8000/         # 302 redirect to /accounts/login/ (auth working)
curl -s http://localhost:8000/display/ # 200 OK (no auth required for display)

# On the Chromebook (or any tailnet device)
curl -s http://wentworth-kitchen:8000/display/ | head -20   # confirm tailnet routing

# Check first cron run
ls -la ~/my-saas/logs/                  # invoice_batch_*.log appears at next hourly mark
```

Wait until at least one full hourly cron cycle has fired (at the top of the next hour). Inspect the log — should match the same shape as the logs on Chromebook.

---

## 11. Rollback plan

If anything goes sideways, the Chromebook is still functional and ready to take the load back:

1. Stop the Pi's services: `sudo systemctl stop django.service && crontab -r`.
2. On the Chromebook, restart the dev server (`python manage.py runserver 0.0.0.0:8000`) and re-enable the existing cron.
3. Point the Apolosign back at `http://penguin:8000/display/`.

Your data on the Pi vs. the Chromebook will diverge from the moment the Pi takes over. To re-sync after rollback, rsync the Pi's state back: `rsync -av --update sean@wentworth-kitchen:~/my-saas/{.ocr_cache,.invoice_totals,db.sqlite3} ~/my-saas/`. Use `--update` so the newer files win.

---

## 12. Ongoing operational notes

### Daily backup

Add to crontab:
```
30 3 * * * tar -czf /home/sean/backups/my-saas-$(date +\%Y\%m\%d).tar.gz -C /home/sean/my-saas db.sqlite3 .ocr_cache .invoice_totals
30 4 * * * find /home/sean/backups/ -name "my-saas-*.tar.gz" -mtime +7 -delete
```

Once OneDrive integration is wired (per `project_immediate_strategy.md`), point this at OneDrive instead of local disk.

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

Once the Pi has been stable for 1–2 weeks of cron + display, the Chromebook can be fully retired from the tailnet. Don't rush this — the Pi backup is the rollback path.

---

## 13. Post-migration TODOs (medium-term)

- **Postgres migration** when Albert starts using the system simultaneously with you. SQLite handles single-writer fine; concurrent writes are a sharp edge. ~2 hr migration when needed.
- **Gunicorn instead of runserver.** `runserver` works but is officially "for development". Pre-staged config at `docs/pi-systemd/gunicorn.conf.py`. ~15 min swap.
- **HTTPS / proper auth** if anything on the network is ever exposed beyond Tailscale. Currently fine because Tailscale is the only ingress, but production-grade deployment wants TLS + a real auth flow.
- **Watchdog** — if the Pi reboots unexpectedly, systemd brings Django back. The cron `@reboot sleep 60` catches up the invoice batch. But there's no alert if cron stops firing. A simple healthcheck → Tailscale-shared webhook → Sean's phone is a one-hour add when worth it.

---

## Appendix — what was inventoried before migration

Captured 2026-04-24 from `penguin`:

**Environment variables (10):** `GOOGLE_CREDENTIALS_PATH`, `SPREADSHEET_ID`, `MAPPING_TAB`, `DRIVE_ROOT_FOLDER_ID`, `DRIVE_INBOX_FOLDER_ID`, `DOCAI_PROJECT_ID`, `DOCAI_LOCATION`, `DOCAI_PROCESSOR_ID`, `SECRET_KEY`, `DEBUG`.

**Credentials files (1):** `invoice_processor/credentials/service_account.json` (2.4 KB).

**State payload (~21 MB):** `db.sqlite3` (2.5 MB), `.ocr_cache/` (18 MB / 288 entries), `.invoice_totals/` (20 KB / 4 files), `.historical_stats/` (24 KB), `invoice_processor/mappings/` (244 KB).

**Cron entries:** 7 scheduled (hourly batch + on-reboot batch + weekly budget_sync + daily refresh_totals + 6-hourly mapping_apply + daily mapping_discover + monthly synergy_tab).

**Tailscale tailnet:** 3 nodes — `penguin` (Linux, current host), `alberts-hp` (Windows, offline 2d), `seans-s24-fe` (Android, offline 2d).
