FROM python:3.10-slim

# Установка системных зависимостей
# ffmpeg - для обработки видео
# libpq-dev, gcc - для сборки psycopg2
# curl - для healthcheck
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libpq-dev \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала копируем requirements для кеширования слоев
COPY requirements.txt .
# Устанавливаем зависимости + gunicorn и eventlet для продакшена
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir gunicorn eventlet

# Копируем код приложения
COPY . .

# Создаем папку для локальных файлов (на всякий случай)
RUN mkdir -p storage/uploads storage/saved_files

# Переменная для python, чтобы вывод не буферизировался
ENV PYTHONUNBUFFERED=1

# По умолчанию запускаем веб-сервер (переопределяется в docker-compose для celery)
CMD ["gunicorn", "--worker-class", "eventlet", "-w", "1", "--bind", "0.0.0.0:8000", "main:app"]