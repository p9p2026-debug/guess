FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt || true

COPY photo_guess_game/ ./photo_guess_game/
COPY run_bot.py .

EXPOSE 10000

CMD ["python", "run_bot.py"]
