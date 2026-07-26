FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY photo_guess_game/ ./photo_guess_game/
COPY run_bot.py .

CMD ["python", "run_bot.py"]
