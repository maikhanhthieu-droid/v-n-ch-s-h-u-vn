# Security notes

- Bot tokens are credentials with full control over the bot. Never commit them,
  paste them into issues, or put them on a command line that may be logged.
- If a token has been shared, open `@BotFather`, select the bot, use the token
  management option to revoke/replace it, and use only the new value.
- Configure the replacement as `TELEGRAM_BOT_TOKEN` in the host's secret manager.
- This repository deliberately has no token, `.env` file, or hard-coded chat ID.

