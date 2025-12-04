#/core/database.py
import logging
from peewee import SqliteDatabase, Database
from playhouse.pool import PooledPostgresqlDatabase
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DBConfig

# Настройка логирования для БД
logger = logging.getLogger("Database")

# Инициализация объекта БД (Proxy не используем, так как конфигурация доступна сразу)
db: Database

if DBConfig.USE_POSTGRES:
    db = PooledPostgresqlDatabase(
        DBConfig.POSTGRES_DB,
        user=DBConfig.POSTGRES_USER,
        password=DBConfig.POSTGRES_PASSWORD,
        host=DBConfig.POSTGRES_HOST,
        port=DBConfig.POSTGRES_PORT,
        max_connections=20,
        stale_timeout=300
    )
    logger.info(f"Using Pooled PostgreSQL connection to {DBConfig.POSTGRES_HOST}")
else:
    os.makedirs(os.path.dirname(DBConfig.SQLITE_PATH), exist_ok=True)
    db = SqliteDatabase(DBConfig.SQLITE_PATH)
    logger.info(f"Using SQLite connection at {DBConfig.SQLITE_PATH}")

def close_db():
    if not db.is_closed():
        db.close()