"""Telegram runtime that runs inside Odoo (no external service).

Plain Python, not Odoo models: models/telegram_engine.py runs it from a
cron job, models/telegram_account.py queues the work it executes.
"""
