# Personal Telegram Client for Odoo — Architecture & Phased Build Plan

Status: Phases 1–7 done on Odoo 19.0 Community (version 19.0.4.0.0). Remaining before publishing: Enterprise and Odoo.sh tests, price, vendor details — see docs/RELEASE_CHECKLIST.md.
Target: **Odoo 19.0** (Community for development; Enterprise compatibility verified before release).
Last updated: 2026-10-12

> **Architecture change (2026-10-12).** The first design used a separate "Telegram Gateway"
> service. On request, the product is now **self-contained**: the module does everything
> inside Odoo, with the Telegram library bundled. The gateway project was removed. This
> document describes the self-contained design.

---

## 0. What we are building

An Odoo 19 app that works like Telegram Desktop *inside* Odoo:

- Every Odoo user can connect **their own personal Telegram account** (user account over MTProto — **not a bot**).
- Login with **QR code** (recommended) or phone number + code, and two-step password.
- Contacts, chat list, message history, send/receive text, unread badges, live updates, search.
- Strictly **private per user**: nobody else in Odoo (not even a manager, through the normal UI/RPC) can read another user's Telegram data.
- Sold on the **Odoo Apps Store**; installs by copying the folder, **nothing else to install**; Community and Enterprise; media later.
- Must not get customers' Telegram accounts, or the app's credentials, **banned**.

### Verified local environment

| Item | Value | How verified |
|---|---|---|
| Odoo | 19.0 Community, image `odoo:19.0`, container `odoo-app` | `docker-compose.yml`, `docker ps` |
| Python in container | 3.12.3 | `docker exec odoo-app python3 --version` |
| Database | PostgreSQL 17, container `odoo-db`, DB `odoo` | `docker-compose.yml` |
| Server mode | threaded (no `workers` set), 2 cron threads (default) — production customers often run **prefork** | `odoo.docker.conf` |
| Addons path | `./MyOdooModules` → `/mnt/extra-addons` | `odoo.docker.conf` |
| Odoo source for API checks | `Odoolocal/odoo-19.0` (19.0 FINAL) | `odoo/release.py` |

---

## 1. The key decisions (and why)

### Decision 1 — Everything runs inside Odoo: a cron-driven "engine"

```
 Browser (OWL client action)
      │ JSON-RPC: /telegram/* (status, start, submit, logout, heartbeat)   ▲ bus.bus websocket
      ▼                                                                    │ "telegram/auth_state"
 ┌──────────────────────────────── Odoo 19 ────────────────────────────────┐
 │ HTTP workers: validate input, queue telegram.command, never talk to     │
 │               Telegram, never hold a Telegram connection                │
 │                                                                         │
 │ Cron job "Telegram: Engine" (engine/runner.py), ~45 s per run,          │
 │ re-triggers itself while accounts are live:                             │
 │   • one Telethon client per connected / logging-in account              │
 │   • runs queued commands about twice a second                           │
 │   • saves sessions encrypted on telegram.account.session_data           │
 │   • pushes state changes to the user's browser over the bus             │
 └─────────────────────────────────────────────────────────────────────────┘
      │ MTProto (Telethon, bundled in lib/)
      ▼
 Telegram servers
```

Why this shape:

1. **One connection per session.** Telegram ends a session that is used from two connections at once (`AUTH_KEY_DUPLICATED`). Odoo runs several HTTP workers, so they must never connect. Odoo locks a cron job while it runs, so exactly one engine exists per database: the engine is the only place that talks to Telegram.
2. **Nothing to install.** No extra service, no Docker, no `pip`: the module is copied into the addons folder and installed like any Odoo app.
3. **Fits Odoo's limits.** Odoo restarts the server when a cron thread exceeds its real-time limit (`limit_time_real_cron`, else `limit_time_real`, default 120 s; measured per polling cycle — verified in `odoo/service/server.py`). The engine runs for 40 % of that limit (max 45 s), then re-triggers itself with low priority (20) so other cron jobs run first.

Trade-offs accepted:

- Messages arrive within ~1–2 s while a run is active; between two runs (a gap of about a second, longer if other cron jobs are queued) they wait for the next run, which fetches everything missed (Telethon catch-up from the saved update state).
- Uses one cron thread continuously while accounts are connected. Customers need `max_cron_threads ≥ 2` (Odoo's default).
- Odoo Online still impossible (no custom Python modules there).

### Decision 2 — Telethon, bundled in the module

| Option | License | Status (2026-10) | Verdict |
|---|---|---|---|
| **Telethon 1.45.0** (pure Python) | MIT | Active (repo moved to Codeberg; releases on PyPI) | **Chosen**: pure Python → can be bundled; no native binaries (Apps Store rule R2) |
| TDLib | Boost-1.0 | Active | Native C++ binary: cannot be bundled without "launching executable code"; would need `pip`/system install |
| Pyrogram & forks | LGPL-3.0 | Original archived | Rejected |

Bundled in `lib/` with their licenses: Telethon 1.45.0 (MIT), pyaes 1.6.1 (MIT), rsa 4.9.1 (Apache-2.0), pyasn1 0.6.4 (BSD-2). `engine/vendor.py` loads Telethon, pyaes and rsa from `lib/` (pinned, tested versions) and keeps the server's own `pyasn1` when present (other Odoo dependencies use it). Hashes in `lib/VENDORED.md`.

### Decision 3 — Every customer uses their own `api_id` / `api_hash`

The `api_id`/`api_hash` pair identifies the *application*, and Telegram enforces its terms against "the Telegram account responsible for the app" (ToS §4). Shipping ours in source code sold to everyone would let one abusive buyer get it banned for all customers. The customer's administrator registers their own pair at <https://my.telegram.org> and enters it in Settings (or `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` in the server environment). Same model as self-hosted bridges such as mautrix-telegram.

### Decision 4 — Odoo stores a cache, not a full mirror

Chat list and contacts always; messages for chats as they arrive and as the user scrolls (one page at a time, never "download everything"). Retention cron and purge on disconnect. Smaller database, less personal data in backups, no bulk downloads that look like scraping.

### Decision 5 — Telegram IDs are stored as strings

Odoo 19 has no 64-bit integer field (`fields.Integer` is `int4`). Telegram ids exceed 2³¹ → `fields.Char`; message ordering via a `float8` `sort_key` (message ids fit exactly in a double).

### Decision 6 — Scope of v1

In: private chats, basic groups, supergroups (read + send text), contacts, partner linking, unread counts, live updates, search.
Later: **broadcast channels** (ToS §3.3 requires sponsored-message support when showing channel content), media, message edit/delete, multiple accounts per user.
**Never**: bulk messaging, broadcasts to non-contacts, auto-replies, member scraping, mass contact import, account registration through the API, "ghost mode", feeding Telegram data to AI (ToS §1.5).

---

## 2. Authentication & session isolation

### Login flows

```
Intro + consent (ToS §2.2) ─┬─ QR:    start_qr → auth.exportLoginToken → QR shown (refreshed ~30 s)
                            │         user scans in Telegram (Settings › Devices › Link Desktop Device)
                            │         → updateLoginToken → export again → success (or DC migration + import)
                            └─ Phone: phone number → send_code → code → sign_in
                                      ↘ SessionPasswordNeeded → two-step password → sign_in(password)
Refused: unknown number (sign-up required), login email setup required, banned number
QR not scanned within 5 minutes → back to start
Logout (user or admin) → auth.logOut (session leaves the user's devices) → session + cache deleted
Ended from the phone → detected at the next connection → disconnected + cache deleted
```

### Rules for codes and passwords

- Browser → `/telegram/auth/submit` (POST body, never the URL) → validated → queued **encrypted** in `telegram.command` → the engine decrypts it, **deletes the command before running it**, and never logs it.
- Login attempts are capped per account (persisted, not reset by logout): code requests 3/day (configurable), code checks 5/hour, password checks 5/hour, QR starts 10/hour.
- A test asserts that phone numbers, codes and passwords never appear in any log record.

### How users are kept apart

| Layer | Mechanism |
|---|---|
| Browser → Odoo | Routes never accept an account or user id; the account is the caller's own. |
| ORM | ACL + record rules; chats/messages/contacts under **global** owner-only rules. No `sudo()` in user-facing reads. |
| Session data | `session_data`, `qr_link`, `login_attempts` are `groups="base.group_system"` fields; `telegram.command` has no user access at all. |
| Telegram | Only the engine connects; one Telethon client per account; Odoo's cron lock guarantees a single engine per database. |

### Encryption at rest

Session data (authorization key, data center, update state, cached access hashes) is encrypted with Fernet (AES-128-CBC + HMAC-SHA256, from Odoo's `cryptography` dependency) under a key derived by HKDF from:

- `TELEGRAM_SESSION_KEY` in the Odoo server's environment (**recommended**: a database dump alone cannot open sessions), else
- the database's own `database.secret` (protects against casual access, not against someone holding the whole database).

Both keys are tried when decrypting, so adding the environment key later keeps sessions working. A session that no key can open is dropped with a clear "connect again" message.

---

## 3. Security model in Odoo

| Group | Can do |
|---|---|
| **Telegram / User** (implies Internal User) | Connect/disconnect *own* account; read *own* chats, messages, contacts; link own contacts to Odoo partners. |
| **Telegram / Administrator** | Settings (with Settings rights), connection status of accounts in their companies, force disconnect. **No access to other users' conversations.** |

- Private methods (`_tg_*`, `_cron_run`) are refused over RPC (tested).
- `sudo()` only in: account creation and engine callbacks (system-managed records), the engine itself (runs as superuser in cron), each with a comment.
- Message bodies rendered as plain text (`t-out`), never HTML.
- Honest limitation (in docs): a database superuser or someone with server shell access can always read the database; with `TELEGRAM_SESSION_KEY` set, they still cannot decrypt sessions from the database alone.

---

## 4. Anti-ban rules (built into the product, not left to luck)

Telegram states that "all accounts that log in using unofficial Telegram API clients are automatically put under observation" and that flooding, spamming or faking counters means being "banned forever". ToS §1.4 also forbids "making actions on behalf of the user without the user's knowledge and consent".

**A. Only human-initiated actions**
1. Every outgoing message comes from a user clicking *Send*. No scheduled sends, no auto-replies, no "send to many", no API for other modules to message arbitrary numbers (v1).
2. No member scraping, no mass `importContacts`; adding a contact by phone is one at a time with a daily cap.
3. Never register new Telegram accounts through the app (sign-up required → refused with a message).

**B. Behave like a normal client**
4. One long-lived session per account, saved encrypted; runs end and resume **without logging in again**.
5. Only the engine connects, and only one engine runs per database → never `AUTH_KEY_DUPLICATED`.
6. Honest device info: `device_model = "Odoo"`, `app_version = "Unofficial Telegram Chat <version>"`, real `system_version`. Never impersonate official apps or reuse their `api_id`.
7. Online status mirrors reality: online while the Telegram screen is open and visible (browser heartbeat every 30 s), never forced on 24/7. No "offline" is ever sent without a prior "online" (each status update also refreshes "last seen").
8. Messages marked read only when the user has the chat open (Phase 4). No "read without receipt".
9. History loaded lazily; contacts synced once then kept current from updates.

**C. Respect limits**
10. `FLOOD_WAIT` is reported to the user ("Telegram asked to wait N seconds") — `flood_sleep_threshold=0`, nothing retries in a loop.
11. Local caps under Telegram's limits (login attempts now; sending and history paging in Phase 4).
12. Failed sends become `failed` with a *Retry* button — no automatic resend storms.

**D. Customer guidance**
13. Established accounts with real SIM numbers; new or virtual numbers are restricted far more often.
14. Stable server/IP; the first login triggers Telegram's normal "new login" notice — expected.
15. Develop on **Telegram's test servers** (Settings › *Use Telegram Test Servers*), numbers `99966XYYYY` (X = DC 1–3, code = X five times). Telegram recommends test DCs "to avoid reaching flood limits".
16. If limited: `@SpamBot` in Telegram; wrongful bans: `recover@telegram.org` (written by a person).

**E. Protect the `api_id`** — 17. Per-customer `api_id`, never in code, never sent to the browser; never Telegram's published sample credentials (`API_ID_PUBLISHED_FLOOD`).
**F. Data use** — 18. Telegram data never goes to AI/LLM features (ToS §1.5).
**G. Login hygiene** — 19. Login attempts capped (Telegram allows about 5 code requests per number per day); the engine stops and explains instead of retrying.

---

## 5. Folder structure

```
MyOdooModules/telegram_personal_client/
├── __init__.py, __manifest__.py    name "Unofficial Telegram Chat", 19.0.2.0.0, OPL-1, depends base_setup/web/bus
├── LICENSE, README.md
├── lib/                            bundled pure-Python libraries + LICENSES/ + VENDORED.md (versions, sha256)
│   ├── telethon/  rsa/  pyaes/  pyasn1/
├── engine/                         Telegram runtime (plain Python, not models)
│   ├── vendor.py                   makes lib/ importable (pinned Telethon, server's pyasn1 preferred)
│   ├── cipher.py                   Fernet encryption, keys from TELEGRAM_SESSION_KEY / database.secret
│   ├── session.py                  DbSession: Telethon session ⇄ JSON dict (stored encrypted)
│   ├── errors.py                   Telethon exceptions → error codes
│   └── runner.py                   Engine: run window, clients, commands, login steps, online status, saving
├── models/
│   ├── telegram_account.py         account per user; login requests; status; error messages; reset
│   ├── telegram_command.py         queue between web requests and the engine (encrypted payloads)
│   ├── telegram_engine.py          cron entry point; run window from Odoo's time limits
│   ├── telegram_chat.py, telegram_message.py, telegram_contact.py   cached Telegram data
│   └── res_config_settings.py      API ID, API hash (write-only), test servers, last engine run
├── data/ir_cron.xml                "Telegram: Engine" (every minute, priority 20, self-re-triggering)
├── controllers/main.py             /telegram/auth/{status,start,submit,logout}, /telegram/heartbeat
├── security/                       privilege + groups + record rules; access rights
├── views/                          menus, client action, accounts (status only), settings
├── static/src/                     OWL: telegram_app (root, bus, heartbeat), connect_wizard; SCSS
├── static/description/icon.png     original icon (not Telegram's logo)
└── tests/                          40 tests: security, RPC, login requests, engine (with a fake Telegram)
```

Phase 4 adds: chat/message/contact sync in `engine/` (event handlers, history paging, send), partner smart button, retention cron; Phase 5 the full chat UI.

---

## 6. Data model

**telegram.account** — `user_id` (unique), `company_id`, `state` (`disconnected`, `waiting_phone`, `waiting_qr`, `waiting_code`, `waiting_password`, `connected`), `phone_masked`, `tg_user_id`, `tg_username`, `tg_name`, `password_hint`, `code_delivery`, `last_error` (+ `_detail`, `_retry`), `connected_since`; system-only: `session_data` (encrypted), `qr_link` (short-lived), `login_attempts` (JSON).

**telegram.command** — `account_id`, `action` (`send_code`, `check_code`, `check_password`, `start_qr`, `logout`, `heartbeat`; Phase 4 adds send/history/read), `payload` (encrypted). Deleted by the engine as soon as it reads it.

**telegram.chat / telegram.message / telegram.contact** — unchanged from Phase 2 (see the models): Telegram ids as Char, `sort_key` float8 for message order, `UNIQUE` constraints per account/chat, `content_type` + `content_meta` ready for media, `partner_id` link on contacts.

---

## 7. Engine ↔ web requests

| Web request | Queues | Engine does |
|---|---|---|
| `/telegram/auth/start` `method=qr` | `start_qr` | export login token → `waiting_qr`, QR link stored (system-only) |
| `/telegram/auth/start` `method=phone` | — | account set to `waiting_phone` |
| `/telegram/auth/submit` `step=phone` | `send_code` (encrypted) | `send_code_request` → `waiting_code`, delivery method, masked phone |
| `step=code` | `check_code` (encrypted) | `sign_in` → `connected`, or `waiting_password` + hint |
| `step=password` | `check_password` (encrypted) | `sign_in(password=…)` → `connected` |
| `/telegram/auth/logout`, admin *Force Disconnect* | `logout` | `log_out` → reset (session + cache deleted) |
| `/telegram/heartbeat` (every 30 s while visible) | `heartbeat` (deduplicated) | keeps the user online for 70 s |

Every state change is pushed with `user._bus_send('telegram/auth_state', …)`; the browser then re-reads `/telegram/auth/status` (which renders the QR image server-side from the stored link). While a command is pending, the browser also re-polls every 3 s in case a bus message is missed.

Failure handling: a failed commit (e.g. concurrent update) rolls back and **re-applies** the engine's changes; executed commands are remembered so a code is never sent twice; if Telegram is unreachable, queued login steps are answered with an error instead of hanging.

---

## 8. Phase-by-phase plan

Common commands (Docker setup of this repo; from Git Bash prefix `MSYS_NO_PATHCONV=1`):

```powershell
docker exec odoo-app odoo -d odoo -u telegram_personal_client --stop-after-init
docker restart odoo-app
docker exec odoo-app odoo -d tg_test -i telegram_personal_client --test-tags /telegram_personal_client --stop-after-init --http-port 8070
docker exec odoo-db dropdb -U odoo --force tg_test
```

### Phase 1 — Requirements & architecture ✅
### Phase 2 — Minimal installable addon ✅ (2026-10-12)
Models, groups, record rules, menus, placeholder screen, security tests. Retention setting moved to Phase 4.

### Phase 3 — Telegram login & sessions ✅ (2026-10-12)
Self-contained engine, bundled Telethon, encrypted sessions, QR + phone + two-step login, logout / force disconnect / remote-termination detection, online heartbeat, login caps, settings (API ID / write-only hash / test servers / last engine run).
Verified: 40/40 tests green on a fresh install; upgrade of the dev database from 19.0.1.0.0 clean; uninstall/upgrade free of warnings; **real Telethon against Telegram's test servers** (dummy credentials) — connection, encrypted session save and error path (`API_ID_INVALID`) work end to end.
Still needs from you: a real `api_id`/`api_hash` to complete a login on the test servers, then with a spare real account.

### Phase 4 — Contacts & conversations ✅ (2026-10-12)
`engine/sync.py` (Telethon → rows) and `engine/messaging.py`: chat list sync (100 most recent, re-synced every 6 h, otherwise kept current by updates), contacts sync (daily), small avatars (2 per tick, never retried in a loop), raw updates for new/edited/deleted messages and read receipts (collected by the handler, applied in order each tick), history pages of 50 (first from the newest, then below the oldest cached), sending with `parse_mode=None` and an idempotent `local_uuid`, read receipts only when the chat is open, open chat from contacts. Large groups cache messages only once opened. Pacing: 20 sends/min, 30 history pages/min, 1 page per chat per 2 s. Retention cron + setting. Broadcast channels skipped.
Verified: 53/53 tests (chat list, previews, contacts, avatars, live updates without duplicates, large-group rule, send + echo dedup, failure + retry, pacing, paging offsets, read receipts, edits/deletes, open chat, cross-user isolation, retention).
Found and fixed while testing: the cron records must be `noupdate` — the engine job is almost always running, and Odoo 19 refuses to modify a running job, which made module upgrades fail.

### Phase 5 — Frontend (OWL) ✅ (2026-10-12)
Two-pane layout (one pane with a back button on phones), chat list with search, avatars/initials, unread badges and pins, conversation with day separators, sender names in groups, sent/read ticks, failed-send *Retry*, infinite scroll upwards (local pages, then Telegram), composer (Enter sends, Shift+Enter new line, 4096 chars), contacts panel to start chats, live updates over the bus, light and dark mode (both bundles compile). Not yet: desktop notifications, media download (shown as labelled placeholders).

### Phase 6 — Security & testing hardening ✅ (2026-10-13, Community)
- Browser check with Playwright/headless Chromium (`telegram_dev/ui_check.py`): every main flow on desktop and phone, zero console errors (the earlier white page would have been caught here).
- Multi-worker server (`--workers=2 --max-cron-threads=1`): the engine runs in the cron worker and reaches Telegram.
- Independent security review: no critical/high findings. Fixed: secrets readable by no one through Odoo (`NO_ACCESS`), session key bound to the database uuid (+ migration re-encrypting sessions), `neutralize.sql`, login steps de-duplicated and capped before queueing, 2FA hint hidden from managers, uniform answer for missing/foreign ids, partner links limited to visible partners, archived users / users losing access logged out, caps on pending messages and inputs, no kernel version sent to Telegram. 13 new tests.
- Upgrade 19.0.3 → 19.0.4 rehearsed on a copy, then applied to the dev database with the user's real session preserved.
- Pending: Enterprise 19 (incl. dark mode, Enterprise-only), Odoo.sh staging, multi-day real-account soak, load measurement.

### Phase 7 — Commercial packaging ✅ (2026-10-13)
Store page `static/description/index.html` (English, no JavaScript, no external links, unofficial and API notices, monetisation, requirements, limitations), cover `banner.png`, six screenshots from made-up demo data, `COPYRIGHT`, translation template (273 strings), admin README with troubleshooting, `docs/RELEASE_CHECKLIST.md`. Pending owner decisions: price, vendor details, apps@odoo.com confirmation, Git repository registration.

---

## 9. Telegram terms, licensing and Apps Store requirements

Verified 2026-10-12 against primary sources. Re-check before release; these pages change without notice.

### 9.1 Telegram API Terms of Service — <https://core.telegram.org/api/terms>

| Rule | What it means for us |
|---|---|
| §1.1 guard users' privacy "with utmost care" | Private-by-default design, encrypted sessions, nothing visible to other users |
| §1.4 no "actions on behalf of the user without the user's knowledge and consent"; no tampering with online/read/typing status | Human-initiated sends only; truthful online/read status (§4 rules 1, 7, 8) |
| §1.5 no use of Telegram data for training/fine-tuning/deploying AI models | Never feed Telegram data to Odoo AI or any LLM (§4 rule 18) |
| §2.1 "You must obtain your own api_id for your application" | Per-customer `api_id` (Decision 3) |
| §2.2 users must know the app uses the Telegram API, "featured prominently" in store descriptions and the in-app intro | Store page + connect-wizard intro |
| §2.3 "the title of your app must not include the word 'Telegram'… unless preceded with the word 'Unofficial'" | App name **"Unofficial Telegram Chat"** (24 chars — fits the store's 25-char limit) |
| §2.4 must not use the official Telegram logo | Original icon |
| §3.1–3.2 monetisation allowed; all methods must be stated in store descriptions | "Paid app" statement on the store page |
| §3.3 apps that show channel content must support official sponsored messages | Channels out of v1 (Decision 6) |
| §4 notice to the responsible account; access cut if not fixed in 10 days | Another reason the customer owns their `api_id` |

From <https://core.telegram.org/api/obtaining_api_id>: one `api_id` per phone number; client libraries are "strictly monitored"; flooding/spam/fake counters → "banned forever"; accounts using unofficial clients are "automatically put under observation"; sample credentials are rate-limited (`API_ID_PUBLISHED_FLOOD`).
From <https://core.telegram.org/api/auth>: three test DCs, test numbers `99966XYYYY`; daily login-attempt limit per phone; some countries require a Premium purchase that only official clients can complete; frozen accounts are read-only.
From <https://core.telegram.org/api/errors>: `FLOOD_WAIT_X` (wait X seconds); `AUTH_KEY_DUPLICATED` when one session is used from two connections in parallel — the user must log in again.

### 9.2 Licenses of what we ship

| Component | License | Obligation |
|---|---|---|
| Telethon 1.45.0 (bundled) | MIT | Keep copyright + license text (`lib/LICENSES/telethon-LICENSE.txt`) |
| pyaes 1.6.1 (bundled) | MIT | Keep license text |
| rsa 4.9.1 (bundled) | Apache-2.0 | Keep license text (checked: the project ships no NOTICE file) |
| pyasn1 0.6.4 (bundled fallback) | BSD-2-Clause | Keep license text |
| qrcode, cryptography | Odoo's own dependencies | Not shipped by us |
| Our addon | OPL-1 | Buyers may modify, not redistribute |

All bundled licenses are permissive and compatible with selling the module under OPL-1. No GPL code is used.

### 9.3 Odoo Apps Store — <https://apps.odoo.com/apps/vendor-guidelines>, <https://apps.odoo.com/apps/faq>

- Licenses accepted: GPL-3, AGPL-3, LGPL-3, OPL-1, other OSI, other proprietary. OPL-1 is Odoo's recommendation for proprietary apps.
- Submission: Git repository (SSH URL) with one branch per Odoo version (`#19.0`); private repos grant read access to the `online-odoo` GitHub user. Each Odoo version is a separate product.
- Page: `static/description/icon.png`, `static/description/index.html`, cover via `images`; English only; no JavaScript; external links only to YouTube/Teams; name ≤ 25 characters.
- Price: manifest `price`/`currency`, EUR or USD, minimum 9 EUR; the store price must be the lowest anywhere; Odoo takes 30%.
- R2: modules that download code, install or launch other executables, or contain obfuscated/encrypted code are removed → we bundle **plain, readable Python source** only; nothing is downloaded or launched.
- R4: data collection must be disclosed; data sent to another service needs user opt-in → connect-wizard intro and explicit *Connect*; we (the vendor) collect nothing.
- No activation keys; must be installable by copying into the addons folder (✅ now literally true).
- Review: not every module is reviewed before publication; enforcement is by unpublishing.

### 9.4 Where customers can run it

| Hosting | Works? |
|---|---|
| On-premise / VPS | ✅ Copy the module, install, enter API ID/hash |
| Odoo.sh | ✅ expected (pure Python, standard cron) — to be confirmed on a staging branch (§9.5) |
| Odoo Online (SaaS) | ❌ Odoo does not allow custom Python modules there |

### 9.5 Still unverified

- Odoo.sh: behaviour of a continuously re-triggered ~45 s cron job, and outbound MTProto (TCP 443) to Telegram → test on a staging branch in Phase 6.
- Prefork servers: engine behaviour with `--workers` and `limit_time_real_cron` → Phase 6.
- Real login on the test servers and on a real account → needs your `api_id`.
- `PEER_FLOOD` is not documented on core.telegram.org; handled generically as a Telegram error.

---

## 10. Risk register

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| 1 | A customer's Telegram account gets limited/banned | Trust, refunds | Anti-ban rules (§4) in code; onboarding guidance; no bulk features |
| 2 | A shared `api_id` gets banned | All customers break | Per-customer `api_id` |
| 3 | Customer sets `max_cron_threads = 0/1` or very low time limits | Engine never runs / starves other jobs | Settings page shows last engine run; docs; window derived from the limits; low priority |
| 4 | Telethon maintenance slows down (repo moved to Codeberg) | Library falls behind Telegram's API layer | Pinned, bundled copy; contract tests with the fake; monitor releases; TDLib remains a fallback design |
| 5 | Database dump leaks sessions | Account takeover | `TELEGRAM_SESSION_KEY` outside the database (documented, recommended) |
| 6 | Privacy / labour-law concerns | Legal | Private by default, admins see status only, retention, purge on disconnect |
| 7 | Event storms from large groups | DB growth, engine load | Metadata only for large groups until opened; batching; retention |
| 8 | Apps Store questions the bundled libraries | Delay | Plain source, licenses included, `VENDORED.md`; ask apps@odoo.com before publishing |
| 9 | Name/branding violates Telegram's terms | Takedown | "Unofficial Telegram Chat", unofficial notice, original icon |
| 10 | Country requires Premium purchase at login | That user cannot connect | Explained by the error message; stated on the store page |
| 11 | Login-attempt limit hit during setup | Locked out for a day | QR first; caps below Telegram's; test servers for development |

---

## 11. Decisions taken

1. Sell **19.0 only** at launch; backport later if demand.
2. Enterprise 19 compatibility claimed only after testing (Phase 6).
3. **No external service**: everything inside Odoo (this document).
4. App name **"Unofficial Telegram Chat"**, menu **"Unofficial Telegram"**.
5. Development on Telegram's test servers first, then a spare real account (not your main one).
6. v1 = private chats + groups; channels later with sponsored-message support.
