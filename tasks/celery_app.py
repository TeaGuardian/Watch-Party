#/tasks/celery_app.py
import os
import sys
from celery import Celery
from celery.signals import task_prerun, task_postrun
from celery.schedules import crontab

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import CeleryConfig
from core.database import db


# Инициализация Celery
app = Celery('watch_party', broker=CeleryConfig.BROKER_URL, backend=CeleryConfig.RESULT_BACKEND)

# Загружаем настройки
app.conf.update(
    task_serializer=CeleryConfig.TASK_SERIALIZER,
    result_serializer=CeleryConfig.RESULT_SERIALIZER,
    accept_content=CeleryConfig.ACCEPT_CONTENT,
    timezone='UTC'
)

# Автоматический поиск задач в модулях
app.conf.imports = ['tasks.media']

# Расписание периодических задач
app.conf.beat_schedule = {
    'cleanup-every-10-minutes': {
        'task': 'tasks.media.cleanup_old_videos',
        'schedule': crontab(minute='*/10'), # Каждые 10 минут
    },
    'check-stuck-every-hour': {
        'task': 'tasks.media.check_stuck_videos',
        'schedule': crontab(minute='*/10'),
    },
    'global-s3-gc-daily': {
        'task': 'tasks.media.daily_s3_garbage_collector',
        'schedule': crontab(hour=4, minute=0),
    },
}

# --- PEEWEE HOOKS ---

@task_prerun.connect
def celery_prerun(*args, **kwargs):
    """Открываем соединение перед выполнением задачи"""
    if db.is_closed():
        db.connect()

@task_postrun.connect
def celery_postrun(*args, **kwargs):
    """Закрываем соединение после выполнения задачи"""
    if not db.is_closed():
        db.close()

        