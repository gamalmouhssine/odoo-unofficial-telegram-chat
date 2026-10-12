# Unofficial Telegram Chat (`telegram_personal_client`)

Each Odoo user connects their own personal Telegram account and chats from inside Odoo.
Unofficial client: not made by or affiliated with Telegram; it uses the Telegram API.

Everything runs inside Odoo: no extra service, no `pip install`. The Telegram library
(Telethon) is bundled in `lib/` (versions and licenses: `lib/VENDORED.md`).

Supported: Odoo 19.0, tested on Community (on-premise; threaded and multi-worker servers).
Not available on Odoo Online (no third-party Python modules there).

## Installation and setup (administrator)

1. Copy `telegram_personal_client` into an addons folder, update the app list and install
   *Unofficial Telegram Chat*.
2. Register your company's app at **my.telegram.org** → *API development tools* (log in
   with a company phone number). One API ID serves every user of the database.
3. In Odoo: *Unofficial Telegram › Configuration › Settings*: enter the **API ID** and
   **API hash** (or set `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` in the Odoo server's
   environment to keep the hash out of the database).
4. **Recommended:** set `TELEGRAM_SESSION_KEY` (32+ random characters) in the Odoo
   server's environment and keep a copy in your password manager. Sessions are then
   encrypted with a key that is not in the database. If the key is lost or changed,
   users simply connect again.
5. Give users the *Unofficial Telegram / User* access right. Administrators get
   *Unofficial Telegram / Administrator* (status of all accounts, force disconnect).

Each user then opens **Unofficial Telegram** and logs in with a QR code (Telegram on the
phone → *Settings › Devices › Link Desktop Device*) or with phone number + code
(+ two-step password if enabled).

### Server requirements

- Outbound internet access from the Odoo server to Telegram (TCP 443).
- `max_cron_threads` ≥ 2 (Odoo's default). The *Telegram: Engine* scheduled action holds
  the Telegram connections and runs almost continuously while accounts are connected
  (runs of about 45 seconds, restarted immediately, low priority so other jobs go first).
- Upgrades: upgrade with Odoo stopped, or on startup (`-u telegram_personal_client`),
  as Odoo recommends.

## How it works

- Web requests never talk to Telegram: they queue commands (`telegram.command`) that the
  engine runs within about half a second. Odoo runs one engine per database (cron lock),
  so a Telegram session is only ever used from one connection, as Telegram requires.
- Sessions are encrypted (Fernet; key derived with HKDF from `TELEGRAM_SESSION_KEY` or the
  database secret, bound to the database uuid) in `telegram.account.session_data`.
- Odoo caches chats and the messages users load or receive; Telegram stays the source of
  truth. Optional retention: *Settings › Message Cache*.

## Privacy and security

- Owner-only global record rules on chats, messages and contacts: no group, administrators
  included, can read another user's Telegram data through Odoo.
- Session data, QR link, login attempts, 2FA hint and queued commands are readable by no
  one through Odoo (`groups=NO_ACCESS`); only the engine (superuser) uses them.
- Login codes and passwords are queued encrypted, deleted as soon as the engine reads them,
  and never logged.
- Duplicated databases cannot open the original's sessions (new database uuid);
  neutralized databases (`data/neutralize.sql`) drop all sessions.
- Archived users, and users who lose the Telegram access right, are logged out within
  about 30 seconds.
- Honest behaviour towards Telegram: no bulk messaging or automation; online status only
  while the screen is open; read receipts only when a chat is opened; flood limits obeyed;
  login attempts capped (3 code requests/day, 5 code or password checks/hour, 10 QR
  starts/hour per account).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "The Telegram API credentials are not configured yet" | Enter API ID and hash in Settings (or the environment variables). |
| Login stays on "Contacting Telegram…" | The engine is not running: check *Settings › Technical › Scheduled Actions › Telegram: Engine* is active, `max_cron_threads` ≥ 1 (2 recommended), and the server can reach Telegram. *Settings › Last Engine Run* shows the last run. |
| "The Telegram API credentials are invalid" | Wrong API ID/hash; copy them again from my.telegram.org. |
| "Telegram asked to wait N seconds" | Telegram's flood limit; wait and retry. |
| "Too many attempts" | The module's protective login caps; wait the indicated time. |
| "This Telegram session was ended…" | The user ended the session from another device; connect again. |
| "…session key changed" | `TELEGRAM_SESSION_KEY` changed or the database was copied; connect again. |
| "Set it up in the official Telegram app first" (login email) | New accounts may need a login email set in the official app before third-party logins. |
| Account limited by Telegram | Check `@SpamBot` in Telegram; wrongful bans: recover@telegram.org (written by a person). |
| Upgrade error "This cron task is currently being executed" | Upgrade with Odoo stopped or at startup. |

## Development

Tests (from Git Bash, prefix with `MSYS_NO_PATHCONV=1`):

```
docker exec odoo-app odoo -d tg_test -i telegram_personal_client --test-tags /telegram_personal_client --stop-after-init --http-port 8070
docker exec odoo-db dropdb -U odoo --force tg_test
```

Browser checks, multi-worker check and store assets: see `telegram_dev/README.md` in the
development repository. For development against Telegram's test servers, enable
*Use Telegram Test Servers* in Settings (test numbers `99966XYYYY`; note that the test
servers' login codes are currently unreliable for third-party clients).
