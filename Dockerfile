FROM python:3.11-slim

# Без этого stdout буферизуется, а при остановке контейнера мы выходим
# через os._exit(0) — весь вывод rich (баннер, предупреждения) терялся.
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 7777

CMD ["python3", "telegram_bot.py"]
