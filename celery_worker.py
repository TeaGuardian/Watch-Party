#/celery_worker.py
import os
import sys

# 1. Добавляем корневую директорию проекта в sys.path
# Это решает проблему "ModuleNotFoundError: No module named 'core'" или 'config'
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from tasks.celery_app import app

if __name__ == '__main__':
    # 2. Формируем аргументы запуска
    # На Windows обязательно использовать --pool=solo или --pool=eventlet
    # solo - проще (однопоточный), eventlet - производительнее (нужен pip install eventlet)

    argv = [
        'worker',
        '--loglevel=INFO',
        '--pool=solo'
    ]

    print("--- Starting Celery Worker (Python Wrapper) ---")
    app.worker_main(argv)