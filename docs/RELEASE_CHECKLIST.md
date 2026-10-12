# Release checklist — Unofficial Telegram Chat (Odoo 19.0)

Status as of 2026-10-13. ✅ done · ⬜ to do before publishing · 🔁 every release

## Product and quality
- ✅ 66 automated tests green on a fresh install (security, login, engine, messaging, hardening)
- ✅ Browser check (`telegram_dev/ui_check.py`) green, no console errors — 🔁 run before each release
- ✅ Multi-worker (prefork) server: engine runs in the cron worker
- ✅ Upgrade 19.0.3 → 19.0.4 with session re-encryption, rehearsed then applied to the dev database (user stayed connected)
- ✅ Uninstall leaves no tables, XML ids or parameters
- ✅ Independent security review; all medium/low findings fixed and covered by tests
- ⬜ **Enterprise 19**: install, run the test suite and the browser check (dark mode is Enterprise-only: check it visually), then add "Enterprise" to the store page requirements
- ⬜ **Odoo.sh**: staging branch — engine runs (continuously re-triggered cron), outbound connection to Telegram works, neutralized staging drops sessions; then mention Odoo.sh on the store page
- ⬜ Real-account soak test: one spare account connected for several days (reconnects, catch-up after server restarts, no unexpected logouts)
- ⬜ Measure engine load with several connected accounts (time per tick, memory) and publish a sizing note

## Apps Store
- ✅ App name ≤ 25 characters, starts with "Unofficial" (Telegram API terms §2.3)
- ✅ Original icon (no Telegram logo, §2.4); cover image `static/description/banner.png` (`images` key)
- ✅ `static/description/index.html`: English, no JavaScript, no external links; unofficial notice and API usage (§2.2); monetisation statement (§3.2); requirements and limitations
- ✅ Screenshots from made-up demo data only
- ✅ License OPL-1 (`LICENSE`), `COPYRIGHT`, bundled licenses in `lib/LICENSES/`, `lib/VENDORED.md` with sha256 of each library
- ✅ No NOTICE files required by bundled libraries (checked: Telethon, pyaes, rsa, pyasn1)
- ✅ Translation template `i18n/telegram_personal_client.pot` — 🔁 regenerate when texts change
- ⬜ Decide the **price** (manifest `price` + `currency`, EUR or USD, minimum 9 EUR, must be the lowest price anywhere)
- ⬜ Confirm vendor name/website in the manifest (`author`, `website`) and set up support contact on the vendor profile
- ⬜ Email apps@odoo.com: bundled pure-Python libraries in `lib/` (no executables, nothing downloaded) — ask for confirmation before publishing
- ⬜ Git repository with branch `19.0`, module at the root; grant read access to `online-odoo` if private; register the repository on apps.odoo.com

## Every release 🔁
- Bump `version` (19.0.x.y.z); add a migration script when stored data changes
- Keep cron records in `<data noupdate="1">` (Odoo refuses to modify the running engine job)
- Fresh install + upgrade from the previous version + tests + browser check
- Package without `__pycache__` (see `.gitignore`)
- Re-check Telegram API terms and Odoo Apps rules for changes
