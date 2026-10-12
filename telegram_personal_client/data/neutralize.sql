-- Neutralized copies (staging, duplicates, tests) must never use the
-- production Telegram sessions: two databases driving the same session
-- makes Telegram end it, and the copy could send messages as real users.
UPDATE telegram_account
   SET session_data = NULL,
       qr_link = NULL,
       state = 'disconnected',
       last_error = NULL;
DELETE FROM telegram_command;
