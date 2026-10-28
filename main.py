"""Entry point for hosts that expect main.py (e.g. Botkeep's default
startup command `python main.py`).

This file does nothing on its own -- it just runs bot.py, which starts
the Telegram bot (polling or webhook, per the admin panel) and the web
admin panel. You can also run `python bot.py` directly; both are identical.
"""

import runpy
import sys

if __name__ == "__main__":
    sys.argv[0] = "bot.py"
    runpy.run_path("bot.py", run_name="__main__")
