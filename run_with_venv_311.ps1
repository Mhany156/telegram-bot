# ---- Run bot in a Python 3.11 virtual environment ----
# Requirements: Python 3.11 installed with "Add to PATH" checked

# Create and activate venv
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1

# Upgrade tooling and install deps
python -m pip install --upgrade pip wheel setuptools
pip install -r requirements.txt

# Run
python bot.py
