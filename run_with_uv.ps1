# ---- Run bot with uv (auto-installs Python 3.11) ----
# 1) Put your real values below, save, then right‑click this file > Run with PowerShell

$env:TELEGRAM_TOKEN="8259412280:AAHE8kBj0OJeVO6Di4c13ScyskGcLw23Zag"
$env:ADMIN_IDS="7361826186"   # your Telegram user_id (comma-separated if multiple admins)

# install uv (safe, fast package manager/runtime)
irm https://astral.sh/uv/install.ps1 | iex

# run the bot (uv will download Python 3.11 if needed)
uv run --python 3.11 -r requirements.txt bot.py
