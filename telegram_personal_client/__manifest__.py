{
    "name": "Unofficial Telegram Chat",
    "summary": "Use your personal Telegram account inside Odoo. "
               "Unofficial client built on the Telegram API, private per user.",
    "description": """
Unofficial Telegram client for Odoo. Each user connects their own Telegram
account; everything runs inside Odoo, no extra service to install. Not made
by or affiliated with Telegram.
""",
    "version": "19.0.4.0.0",
    "category": "Productivity",
    "license": "OPL-1",
    "author": "Baam",
    "website": "https://baam-construction.ma",
    "depends": ["base_setup", "web", "bus"],
    # Part of Odoo's standard requirements. Telethon and its dependencies
    # are bundled in lib/ (see lib/VENDORED.md).
    "external_dependencies": {"python": ["qrcode", "cryptography"]},
    "data": [
        "security/telegram_security.xml",
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "views/telegram_account_views.xml",
        "views/res_config_settings_views.xml",
        "views/telegram_menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "telegram_personal_client/static/src/**/*",
        ],
    },
    "images": ["static/description/banner.png"],
    # Odoo Apps Store: one-time price per Odoo version. Must be the lowest
    # price this app is sold for anywhere.
    "price": 79.0,
    "currency": "EUR",
    "application": True,
    "installable": True,
}
