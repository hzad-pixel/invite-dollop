# Playwright image already includes Chromium + system libraries.
# This is why the bot works in Docker/VPS even though plain Python hosts
# (Render free / Botkeep) may not provide Chromium -- see README.
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "bot.py"]
