# tg-job-bot
24x7 Telegram job alert + quiz bot for SSC, RRB, Bihar and Army channels. Runs only on free GitHub Actions
(public repo = unlimited free minutes). No server, no outside service.

- `.github/workflows/bot.yml` runs a **loop**: every ~10 min `bot.py tick` checks official sites + backup
  aggregators, posts new notices (inline buttons: Official Notice / Website / Apply / Group), answers group doubts,
  and runs each daily job once at its IST time:
  07:30 CA quiz, 08:30 + 18:30 current-affairs post, 09:30 last-date reminders, 12:30 GK, 17:30 roundup,
  19:55 main quiz, 21:30 tip.
- After ~5h40m the loop starts the next loop itself. A cron (twice an hour) restarts it if the chain ever breaks.
- Manual run: Actions -> Job Bot 24x7 -> Run workflow -> mode (`selftest` posts nothing).
- Secrets: `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY` (free). Without AI it still posts alerts and uses `quiz_bank.json`.
- Channels and sources: `config.json`. Seen posts + daily-job marks: `state/seen.json` (old items are seeded silently).
