# Dining-Hall TV Setup Playbook

Setup guide for the Hisense 55" Class S7 CanvasTV (4K QLED, Google TV) being mounted at Synergy's dining hall to display the resident menu.

Created 2026-05-12 (night-before the mount).

---

## URL for the TV

Resident-facing menu display, hosted from KitchenPi on the Wentworth LAN:

```
http://kitchenpi.local:8000/display/?audience=residents
```

IP fallback if mDNS doesn't resolve on the TV:

```
http://192.168.1.98:8000/display/?audience=residents
```

The `?audience=residents` flag hides the back-of-house bits (prep status, staff
assignee names, headcount) and changes the header to "Today's Menu · Wentworth"
instead of "Kitchen · Wentworth". See commit `471892d` (B-DisplayResidentMode).

---

## 0. Pre-flight (do tonight or first thing AM, 5 min)

- [ ] Verify the URL works from any device already on Wentworth WiFi — laptop or phone browser:
      `http://kitchenpi.local:8000/display/?audience=residents`
- [ ] If mDNS fails, try the IP: `http://192.168.1.98:8000/display/?audience=residents`
- [ ] Decide: shared Synergy Google account or Sean's personal? Synergy is cleaner
      long-term (IT can take over later), personal is faster today. Either works.

---

## 1. Power on + Google TV first-boot (~10 min)

1. Plug in TV, turn it on.
2. Pair the Hisense remote (TV prompts; press **Home + Back** for ~5s).
3. **Language → English → Region → United States**.
4. **Set Up Your Google TV** — choose **Set up Google TV** (full Android experience
   with Play Store + Chrome). The "basic TV" path skips Play Store and you'll
   regret it.
5. Sign in with the chosen Google account.
6. Skip streaming-app setup (Netflix etc) unless someone explicitly wants it.
7. Connect to Wentworth WiFi — same network the Pi is on.
8. Accept ToS, skip personalization.

---

## 2. Get a browser ready for kiosk mode (~5 min)

Stock Chrome works for testing but is awkward for permanent kiosk use. Install
**Fully Kiosk Browser** (free trial, ~$10 one-time for full version) — it's the
standard kiosk-display Android browser:

- Auto-launch on device startup
- Auto-reload on crash
- Hides Android nav bar (no random taps closing it)
- Wakes screen at scheduled times if you want

### Install + configure

1. Home button → **Apps** → **Search** → type "Fully Kiosk Browser".
2. Install. Open. Accept permissions when prompted.
3. **Settings → Start URL:** paste:
   ```
   http://kitchenpi.local:8000/display/?audience=residents
   ```
4. **Settings → Run on Device Startup:** ON.
5. **Settings → Keep Screen On:** ON.
6. **Settings → Fullscreen Mode:** ON.
7. **Settings → Auto-Reload on Idle Time:** 60 seconds (matches page's built-in refresh).
8. Save → tap **START** at the top.

### Quick-test alternative

Stock Chrome works for a fast verify — open the URL, see the menu. But you'll
need to manually re-launch after every power cycle. Use Fully for the permanent install.

---

## 3. Verify the display (~5 min)

Walk to where residents will sit/stand. Check:

- [ ] **Date + day** in upper-left reads today (e.g. "Wednesday · May 13, 2026").
- [ ] **4 meal slots** show in a row across the top (Cold Breakfast / Hot Breakfast / Lunch / Dinner).
- [ ] **● NOW badge** is on the slot matching the current time (e.g. lunch noon-5pm).
- [ ] **Dish names** legible from the viewing position.
- [ ] **Allergen pills** show on dishes that have them.
- [ ] **No "Prep: X/Y done" anywhere** (that confirms resident mode IS active).
- [ ] **No staff names (sean/albert) on dishes** (same).
- [ ] **"Coming up this week" strip** shows at the bottom with 6 days.
- [ ] Header reads **"Today's Menu · Wentworth"** (NOT "Kitchen · Wentworth").

If header still says "Kitchen · Wentworth" → URL got pasted without
`?audience=residents`. Re-paste carefully.

---

## 4. Test the 60s auto-refresh

1. From your laptop/phone, open `/menus/` or `/menu_planner/` (admin side).
2. Edit any of today's dishes — change the freetext name slightly.
3. Save.
4. Wait up to 60 seconds at the TV.
5. The TV should update without anyone touching it.

If it doesn't refresh: in Fully Kiosk Browser, **Settings → Web Auto Reload → 60**.
Also can force a manual reload by long-pressing the screen or pressing the
remote menu button.

---

## 5. Mount + cable considerations

(Synergy IT/install team probably handles this, but flagging:)

- **No HDMI needed** — TV runs everything internally over WiFi.
- **Power outlet** must be reachable (or use an extension cord — TV power draw
  is ~150W for 55" QLED).
- **WiFi signal at the mount location** — check before drilling. If signal is
  weak, consider a small WiFi extender.

---

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `kitchenpi.local` won't load | Android mDNS misbehavior on this network | Use IP: `http://192.168.1.98:8000/display/?audience=residents` |
| IP fallback also fails | Pi unreachable from TV's network segment | Confirm both on same SSID/VLAN. Some guest WiFi networks isolate clients — switch to main WiFi. |
| Loads but says "No menu scheduled" | No `Menu` rows for today | Author via `/menu_planner/`, wait 60s |
| TV reboots and shows the home screen | Fully's "Run on Device Startup" didn't stick | Re-check setting; also try **Settings → Device Owner / Kiosk Mode** for stronger lockdown |
| IP changes after router reboot (192.168.1.98 stops working) | DHCP lease shifted | Ask Synergy IT to reserve `kitchenpi.local`'s MAC at `192.168.1.98` in the router config — permanent fix |
| Text too small from across the room | 55" + viewing distance | Crank Google TV's display zoom (Settings → Accessibility → Display Size), or ping Claude to bump font sizes on `/display/?audience=residents` specifically |

---

## 7. After it's up — the read test

Walk to the table furthest from the TV, look at the screen, ask:

- Can I read the dish names? ← **most important**
- Can I tell what's coming up tomorrow?
- Does it feel like a menu, or a dashboard?

Anything that fails the read test → flag it. The resident-mode rendering can
be tuned (bigger fonts, fewer dishes per strip, simpler color scheme — whatever).

---

## IP-boundary note (from project_external_signage memo)

This is the **dining-hall internal-use case** (Option 2 from the bookmarked
memo). Same posture as the Apolosign already running in the kitchen. Lower IP
risk than external-facing variants. **One caveat:** if tour groups regularly
pass through the dining hall, the menu becomes visible to non-resident
outsiders — that intersects the tour-display concern in `project_ip_ownership.md`.
Probably benign for menu content alone, but worth a mental flag.

---

## Reference: what `/display/?audience=residents` shows

Designed for the 55" TV at viewing distance:

- Today's 4 meal slots (cold breakfast / hot breakfast / lunch / dinner) with dish names + recipe titles
- "● NOW" current-slot indicator (auto-detects based on time of day)
- Allergen pills on each dish (resident-relevant)
- Clock (when viewing today)
- 6-day "Coming up this week" strip at the bottom
- 60s HTMX auto-refresh (page updates as menus get edited)

Hidden in resident mode:

- "Kitchen · Wentworth" header → swapped to "Today's Menu · Wentworth"
- Page title "Kitchen — ..." → swapped to "Menu — ..."
- Residents headcount block (top-right)
- Prep done count
- Per-dish staff assignee badges (sean/albert)
