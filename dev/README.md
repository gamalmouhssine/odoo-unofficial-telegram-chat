# Telegram module — development checks

Not shipped with the module. Everything runs in throw-away containers on an isolated
Docker network (`tgcheck-net`), never against the development database or a real
Telegram account.

| File | Purpose |
|---|---|
| `seed_demo.py` | Odoo shell script: made-up demo users, chats, messages; engine off, dummy API credentials |
| `ui_check.py` | Playwright (headless Chromium) check of the screen: chat list, search, group/private chats, sending, contacts, disconnect dialog, phone layout, login wizard, settings. Fails on any browser console error. Saves screenshots. |
| `prefork_check.py` | Queues a login step on Telegram's test servers with dummy credentials; a multi-worker server must answer `API_ID_INVALID` from its cron worker |
| `make_banner.py` | Apps Store cover image from the icon and a screenshot |
| `tgcheck-odoo.conf` | Config for `odoo` subcommands (e.g. `i18n export`) on the check stack |

## Stack

```bash
export MSYS_NO_PATHCONV=1
docker network create tgcheck-net
docker run -d --name tgcheck-db --network tgcheck-net -e POSTGRES_USER=odoo -e POSTGRES_PASSWORD=odoo -e POSTGRES_DB=postgres postgres:17-alpine
ODOO="docker run --rm -i --network tgcheck-net -e HOST=tgcheck-db -e USER=odoo -e PASSWORD=odoo -v D:/ProjectOdoo/MyOdooModules:/mnt/extra-addons:ro odoo:19.0"
$ODOO odoo -d tg_ui -i telegram_personal_client --stop-after-init --no-http
$ODOO odoo shell -d tg_ui --no-http < telegram_dev/seed_demo.py
docker run -d --name tgcheck-odoo --network tgcheck-net -e HOST=tgcheck-db -e USER=odoo -e PASSWORD=odoo -v D:/ProjectOdoo/MyOdooModules:/mnt/extra-addons:ro odoo:19.0 odoo -d tg_ui --db-filter='^tg_ui$'
```

## Browser check (Playwright 1.63.0)

```bash
docker run --rm --network tgcheck-net -v D:/ProjectOdoo/telegram_dev:/work:ro -v D:/tmp/shots:/out python:3.12-slim bash -c \
  "pip install playwright==1.63.0 && playwright install --with-deps chromium && python /work/ui_check.py http://tgcheck-odoo:8069 tg_ui /out"
```

Restart `tgcheck-odoo` after changing JavaScript (asset bundles are cached).

## Multi-worker engine check

```bash
$ODOO odoo -d tg_prefork -i telegram_personal_client --stop-after-init --no-http
$ODOO odoo shell -d tg_prefork --no-http < telegram_dev/prefork_check.py
docker run -d --name tgcheck-prefork --network tgcheck-net -e HOST=tgcheck-db -e USER=odoo -e PASSWORD=odoo -v D:/ProjectOdoo/MyOdooModules:/mnt/extra-addons:ro odoo:19.0 odoo -d tg_prefork --db-filter='^tg_prefork$' --workers=2 --max-cron-threads=1
docker exec tgcheck-db psql -U odoo -d tg_prefork -tAc "SELECT state, last_error FROM telegram_account"   # expect API_ID_INVALID
```

## Translation template

```bash
docker run --rm --entrypoint odoo --network tgcheck-net -v D:/ProjectOdoo/telegram_dev/tgcheck-odoo.conf:/conf/odoo.conf:ro \
  -v D:/ProjectOdoo/MyOdooModules:/mnt/extra-addons:ro -v D:/ProjectOdoo/MyOdooModules/telegram_personal_client/i18n:/out \
  odoo:19.0 i18n export -c /conf/odoo.conf -d tg_ui -o /out/telegram_personal_client.pot telegram_personal_client
```

## Teardown

```bash
docker rm -f tgcheck-odoo tgcheck-prefork tgcheck-db; docker network rm tgcheck-net
```
