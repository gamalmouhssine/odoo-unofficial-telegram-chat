import logging

from odoo import api, models
from odoo.tools import config

from ..engine.runner import Engine

_logger = logging.getLogger(__name__)

MAX_WINDOW = 45
MIN_WINDOW = 10


class TelegramEngine(models.AbstractModel):
    """Runs the Telegram engine from the "Telegram: Engine" cron job."""
    _name = 'telegram.engine'
    _description = 'Telegram Engine'

    @api.model
    def _run_window_seconds(self):
        """Stay well inside Odoo's real-time limit for cron jobs, which
        restarts the server when exceeded."""
        limit = config['limit_time_real_cron']
        if limit is None or limit < 0:
            limit = config['limit_time_real']
        if not limit:
            return MAX_WINDOW
        return max(MIN_WINDOW, min(MAX_WINDOW, int(limit * 0.4)))

    @api.model
    def _cron_run(self):
        engine = Engine(self.env, self._run_window_seconds(), commit=self.env.cr.commit)
        if engine.run():
            # Accounts are live: start the next window right away. The job's
            # low priority lets every other cron job run in between.
            self.env.ref('telegram_personal_client.ir_cron_telegram_engine')._trigger()
