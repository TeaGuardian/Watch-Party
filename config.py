#/config.py
import os
from dotenv import load_dotenv

# Загружаем переменные из .env файла
load_dotenv()


class AppConfig:
    VERSION = "1.0.4-beta (02.01.2026)"
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-it")
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    UPLOAD_FOLDER = os.path.join(BASE_DIR, "storage", "uploads")
    LOCAL_STORAGE_PATH = os.path.join(BASE_DIR, "storage", "saved_files")
    SHARED_CONTENT_PATH = "shared_content"

    # БЕЗОПАСНОСТЬ
    MAX_OPENED_ROOMS = int(os.getenv("MAX_OPENED_ROOMS", 2))
    MAX_DUPLICATION = int(os.getenv("MAX_DUPLICATION", 2))

    # --- КВОТЫ РЕСУРСОВ ---
    MAX_ROOMS_COUNT = int(os.getenv("MAX_ROOMS_COUNT", 5))
    MAX_ROOM_VIDEOS = int(os.getenv("MAX_ROOM_VIDEOS", 10))

    # Лимит на один файл
    MAX_VIDEO_SIZE_MB = int(os.getenv("MAX_VIDEO_SIZE_MB", 2000))
    MAX_VIDEO_SIZE_BYTES = MAX_VIDEO_SIZE_MB * 1024 * 1024

    # Лимит на общий вес файлов в комнате (по умолчанию 3 ГБ)
    MAX_ROOM_STORAGE_MB = int(os.getenv("MAX_ROOM_STORAGE_MB", 3072))
    MAX_ROOM_STORAGE_BYTES = MAX_ROOM_STORAGE_MB * 1024 * 1024

    MAX_VIDEO_RETENTION_HOURS = int(os.getenv("MAX_VIDEO_RETENTION_HOURS", 4))
    MAX_PROCESSING_TIMEOUT_HOURS = int(os.getenv("MAX_PROCESSING_TIMEOUT_HOURS", 2))

    MAX_CONTENT_LENGTH = MAX_VIDEO_SIZE_BYTES + 10 * 1024 * 1024

    HEAVY_VIDEO_THRESHOLD_MB = int(os.getenv("HEAVY_VIDEO_THRESHOLD_MB", 30))
    HEAVY_VIDEO_THRESHOLD_BYTES = HEAVY_VIDEO_THRESHOLD_MB * 1024 * 1024


class DBConfig:
    # Переключатель: True = PostgreSQL, False = SQLite
    USE_POSTGRES = os.getenv("USE_POSTGRES", "False").lower() == "true"

    POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
    POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", 5432))
    POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
    POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "password")
    POSTGRES_DB = os.getenv("POSTGRES_DB", "watch_service_db")

    SQLITE_PATH = os.path.join(AppConfig.BASE_DIR, "storage", "database.db")


class StorageConfig:
    # Переключатель: True = MinIO/S3, False = Local Disk
    USE_S3 = os.getenv("USE_S3", "False").lower() == "true"

    ENDPOINT = os.getenv("S3_ENDPOINT", "localhost:9000")
    ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "minioadmin")
    SECRET_KEY = os.getenv("S3_SECRET_KEY", "minioadmin")
    BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "watch-content")
    SECURE = os.getenv("S3_SECURE", "False").lower() == "true"  # True для https


class BotConfig:
    TOKEN = os.getenv("BOT_TOKEN", "test")
    LINK = os.getenv("BOT_LINK", "https://t.me/watch2g_bot")
    ADMIN_IDS = [int(x) for x in os.getenv("BOT_ADMIN_IDS", "1525377107").split(",") if x.isdigit()]


class RedisConfig:
    HOST = os.getenv("REDIS_HOST", "127.0.0.1")
    PORT = int(os.getenv("REDIS_PORT", 6379))
    DB = int(os.getenv("REDIS_DB", 0))
    # Формируем URL для подключения
    URL = f"redis://{HOST}:{PORT}/{DB}"


class CeleryConfig:
    BROKER_URL = RedisConfig.URL
    RESULT_BACKEND = RedisConfig.URL
    TASK_SERIALIZER = 'json'
    RESULT_SERIALIZER = 'json'
    ACCEPT_CONTENT = ['json']

    QUEUE_FAST = 'fast_queue'
    QUEUE_HEAVY = 'heavy_queue'

    # Настройки конкурентности
    # Сколько задач обрабатывается параллельно
    FAST_WORKER_CONCURRENCY = int(os.getenv("FAST_WORKER_CONCURRENCY", 3))
    HEAVY_WORKER_CONCURRENCY = int(os.getenv("HEAVY_WORKER_CONCURRENCY", 2))


# Создаем необходимые папки при старте, если их нет
os.makedirs(AppConfig.UPLOAD_FOLDER, exist_ok=True)
if not StorageConfig.USE_S3:
    os.makedirs(AppConfig.LOCAL_STORAGE_PATH, exist_ok=True)