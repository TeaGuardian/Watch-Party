@echo off
title Watch Party Service Launcher
color 0A

:: Переходим в директорию скрипта
cd /d "%~dp0"

echo ========================================================
echo        STARTING WATCH PARTY SERVICE (Windows)
echo ========================================================

:: 1. Проверка виртуального окружения
if not exist "venv" (
    color 0C
    echo [ERROR] Virtual environment 'venv' not found!
    echo Please create it: python -m venv venv
    pause
    exit /b
)

:: 2. Активация venv
call venv\Scripts\activate

:: 3. Проверка библиотек (быстрый фикс твоей ошибки)
echo [CHECK] Checking dependencies...
pip install redis eventlet >nul 2>&1

:: 4. Запуск Celery Worker (через Python скрипт)
echo [START] Launching Celery Worker...
start "Celery Worker" cmd /k "call venv\Scripts\activate && python celery_worker.py"

:: 5. Запуск bot (через Python скрипт)
echo [START] Launching Celery Worker...
start "Bot Worker" cmd /k "call venv\Scripts\activate && python run_bot.py"

:: 6. Запуск основного приложения
echo [START] Launching Main App...
python main.py

pause