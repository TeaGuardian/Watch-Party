# /tasks/media.py
import os
import shutil
import subprocess
import logging
import requests
from uuid import uuid4
from datetime import datetime, timedelta

from tasks.celery_app import app
from core.models import Video, Room
from core.storage import storage
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import AppConfig, RedisConfig

# Настройка логгера для Celery
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


FLASK_INTERNAL_URL = "http://web_app:8000/api/internal"

def send_progress_to_flask(room_uuid, video_id, percent):
    try:
        requests.post(f"{FLASK_INTERNAL_URL}/progress", json={
            'room_uuid': room_uuid,
            'video_id': video_id,
            'percent': percent
        }, timeout=1) # Короткий таймаут, чтобы не тормозить процессинг
    except Exception as e:
        logger.warning(f"Failed to send progress to Flask: {e}")

def send_refresh_to_flask(room_uuid):
    try:
        requests.post(f"{FLASK_INTERNAL_URL}/refresh", json={
            'room_uuid': room_uuid
        }, timeout=1)
    except Exception as e:
        logger.warning(f"Failed to send refresh to Flask: {e}")


def get_video_duration(file_path):
    """Получает длительность видео в секундах через ffprobe"""
    try:
        cmd = [
            'ffprobe',
            '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            file_path
        ]
        # capture_output=True доступен в Python 3.7+
        result = subprocess.run(cmd, capture_output=True, text=True)
        duration = float(result.stdout.strip())
        logger.info(f"Video duration detected: {duration}s")
        return duration
    except Exception as e:
        logger.error(f"FFprobe error: {e}")
        return 0.0


@app.task(name='tasks.media.process_video', bind=True)
def process_video_task(self, video_id: int, local_source_path: str):
    logger.info(f"--- START PROCESSING VIDEO {video_id} ---")
    logger.info(f"Source: {local_source_path}")

    try:
        video = Video.get_by_id(video_id)
    except Exception as e:
        logger.error(f"Video {video_id} not found in DB: {e}")
        if os.path.exists(local_source_path): os.remove(local_source_path)
        return

    video.status = 'processing'
    video.save()

    room_uuid = str(video.room.uuid)

    # Создаем временную папку
    transcode_dir = os.path.join(AppConfig.BASE_DIR, "storage", "temp_transcode", str(uuid4()))
    os.makedirs(transcode_dir, exist_ok=True)

    playlist_name = "index.m3u8"
    output_path = os.path.join(transcode_dir, playlist_name)

    try:
        # 1. Получаем длительность
        total_duration = get_video_duration(local_source_path)
        if total_duration > 0:
            video.duration = int(total_duration)
            video.save()
        else:
            logger.warning("Could not determine video duration, progress might be broken.")

        # 2. Формируем команду FFmpeg
        # -progress pipe:1 заставляет FFmpeg писать машиночитаемый статус в stdout
        # -nostats убирает "человеческий" прогресс из stderr, чтобы не мусорить
        command = [
            'ffmpeg', '-y',
            '-i', local_source_path,
            '-threads', '0',
            '-c:v', 'libx264',
            '-preset', 'veryfast',
            '-crf', '24',
            '-c:a', 'aac', '-b:a', '128k',
            '-hls_time', '6',
            '-hls_playlist_type', 'vod',
            '-hls_segment_filename', os.path.join(transcode_dir, 'segment_%03d.ts'),
            '-progress', 'pipe:1',
            '-nostats',
            output_path
        ]

        logger.info("Starting FFmpeg subprocess...")

        # 3. Запуск процесса
        # bufsize=0 (небуферизированный) не работает с text=True, поэтому используем дефолт,
        # но читаем через readline()
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True  # Декодирует байты в строки автоматически
        )

        last_percent = -1

        # 4. Чтение вывода в реальном времени
        while True:
            # Читаем одну строку
            line = process.stdout.readline()

            # Если строка пустая и процесс завершился - выходим
            if not line and process.poll() is not None:
                break

            if not line:
                continue

            # Парсинг строки вида "out_time_ms=123456"
            # .strip() важен, чтобы убрать \n
            line = line.strip()

            if line.startswith("out_time_ms="):
                try:
                    # Значение может быть "N/A" в начале, игнорируем
                    raw_value = line.split('=')[1]
                    if raw_value == 'N/A': continue

                    time_ms = int(raw_value)
                    current_seconds = time_ms / 1000000.0

                    if total_duration > 0:
                        percent = int((current_seconds / total_duration) * 100)
                        percent = min(99, max(0, percent))

                        # Отправляем обновление только если процент изменился
                        if percent > last_percent:
                            last_percent = percent

                            # ЛОГИРУЕМ каждый 10-й процент, чтобы видеть в консоли Celery
                            if percent % 10 == 0:
                                logger.info(f"Processing progress: {percent}%")

                            send_progress_to_flask(room_uuid, video_id, percent)

                except ValueError:
                    pass  # Игнорируем ошибки парсинга конкретной строки

        # Ждем завершения процесса окончательно
        return_code = process.wait()

        if return_code != 0:
            stderr_out = process.stderr.read()
            logger.error(f"FFmpeg failed with code {return_code}. Stderr: {stderr_out}")
            raise Exception("FFmpeg process failed")

        logger.info("Transcoding finished successfully.")

        # 5. Перенос файлов в хранилище (S3 или Local)
        # Проверяем, не удалил ли юзер видео, пока мы рендерили
        if not Video.select().where(Video.id == video_id).exists():
            logger.warning("Video was deleted from DB during processing. Aborting upload.")
            return

        # Определяем путь назначения
        if video.file_hash:
            storage_folder = f"{AppConfig.SHARED_CONTENT_PATH}/{video.file_hash}/"
        else:
            storage_folder = f"users/{video.room.owner_id}/rooms/{video.room.uuid}/videos/{video.id}/"

        logger.info(f"Uploading files to storage: {storage_folder}")

        # Загружаем все файлы (m3u8 и ts)
        for filename in os.listdir(transcode_dir):
            file_path = os.path.join(transcode_dir, filename)
            if os.path.isfile(file_path):
                with open(file_path, 'rb') as f:
                    content = f.read()

                # Путь в S3/Local
                dest_path = storage_folder + filename

                # Контент-тип
                ctype = 'application/x-mpegURL' if filename.endswith('.m3u8') else 'video/MP2T'

                if not storage.save_file(content, dest_path, content_type=ctype):
                    raise Exception(f"Failed to upload {filename}")

        # 6. Финализация
        video = Video.get_by_id(video_id)
        video.status = 'ready'
        # Слэши важны для URL
        video.storage_path = (storage_folder + playlist_name).replace('\\', '/')
        video.save()

        send_refresh_to_flask(room_uuid)
        logger.info(f"Video {video_id} is READY.")

    except Exception as e:
        logger.exception(f"Processing CRITICAL FAIL: {e}")
        try:
            v = Video.get_or_none(Video.id == video_id)
            if v:
                v.status = 'error'
                v.save()
                send_refresh_to_flask(room_uuid)
        except:
            pass

    finally:
        # Очистка временных файлов
        if os.path.exists(transcode_dir):
            shutil.rmtree(transcode_dir, ignore_errors=True)
        if os.path.exists(local_source_path):
            os.remove(local_source_path)


@app.task(name='tasks.media.delete_storage_folder')
def delete_storage_folder_task(path: str):
    """
    Безопасное удаление папки.
    Если папка относится к shared_content, проверяем БД: не используется ли она?
    """
    if not path:
        return

    clean_path = path.strip("/").replace("\\", "/")
    if not clean_path or clean_path == "." or clean_path == "users" or clean_path == AppConfig.SHARED_CONTENT_PATH:
        logger.critical(f"🛑 ATTEMPT TO DELETE PROTECTED PATH BLOCKED: {path}")
        return

    if AppConfig.SHARED_CONTENT_PATH in clean_path:
        logger.info(f"🛡️ Safety check for shared content: {clean_path}")
        in_use_count = Video.select().where(
            Video.storage_path.startswith(clean_path)
        ).count()

        if in_use_count > 0:
            logger.warning(
                f"🛑 ABORT DELETE: Storage folder '{clean_path}' is still in use by {in_use_count} videos in DB.")
            return

    logger.info(f"Deleting storage folder: {path}")

    # Рекурсивное удаление
    success = storage.delete_folder(path)

    if success:
        logger.info(f"Successfully deleted: {path}")
    else:
        logger.warning(f"Failed to delete (or not found): {path}")


@app.task(name='tasks.media.delete_account_files')
def delete_account_files_task(user_id: int):
    """
    Удаляет личную папку пользователя (аватарки, старые видео).
    НЕ трогает shared_content.
    """
    logger.info(f"Deleting personal files for user {user_id}")
    folder_prefix = f"users/{user_id}/"
    if not str(user_id).isdigit():
        logger.error(f"Invalid user_id: {user_id}")
        return

    success = storage.delete_folder(folder_prefix)

    if success:
        logger.info(f"Successfully deleted files for user {user_id}")
    else:
        logger.error(f"Failed to delete files for user {user_id}")


@app.task(name='tasks.media.cleanup_old_videos')
def cleanup_old_videos_task():
    logger.info("Starting cleanup of old videos...")
    retention_hours = AppConfig.MAX_VIDEO_RETENTION_HOURS
    cutoff_time = datetime.now() - timedelta(hours=retention_hours)

    old_videos = Video.select().where(
        (Video.last_played_at < cutoff_time) &
        (Video.status == 'ready')
    )

    deleted_records = 0
    deleted_files = 0
    affected_rooms = set()

    for video in old_videos:
        try:
            file_hash = video.file_hash
            room_uuid = str(video.room.uuid)
            logger.info(f"Processing cleanup for video {video.id} (hash: {file_hash})")
            other_refs_count = Video.select().where(
                (Video.file_hash == file_hash) &
                (Video.id != video.id)
            ).count()

            if other_refs_count == 0:
                if video.storage_path:
                    folder_path = os.path.dirname(video.storage_path)
                    if not folder_path.endswith('/'): folder_path += '/'

                    logger.info(f"Removing physical files at {folder_path}")
                    if storage.delete_folder(folder_path):
                        deleted_files += 1
            else:
                logger.info(f"Skipping physical deletion (used by {other_refs_count} others)")
            video.delete_instance()
            deleted_records += 1
            affected_rooms.add(room_uuid)

        except Exception as e:
            logger.error(f"Error cleaning video {video.id}: {e}")

    for r_uuid in affected_rooms:
        try:
            # Шлем событие 'playlist_refresh' в комнату
            send_refresh_to_flask(r_uuid)
            logger.info(f"Notified room {r_uuid} about auto-deletion")
        except Exception as e:
            logger.error(f"Failed to emit socket to {r_uuid}: {e}")
    logger.info(f"Cleanup finished. Records removed: {deleted_records}. File groups removed: {deleted_files}.")


@app.task(name='tasks.media.check_stuck_videos')
def check_stuck_videos_task():
    """
    Ищет видео, которые зависли в статусе processing/uploading
    дольше допустимого времени (например, из-за рестарта сервера).
    """
    logger.info("Checking for stuck videos...")

    timeout_hours = AppConfig.MAX_PROCESSING_TIMEOUT_HOURS
    cutoff_time = datetime.now() - timedelta(hours=timeout_hours)

    # Ищем видео, которые "зависли"
    stuck_videos = Video.select().where(
        Video.status.in_(['processing', 'uploading']) &
        (Video.created_at < cutoff_time)
    )

    count = 0
    affected_rooms = set()

    for video in stuck_videos:
        try:
            logger.warning(f"Found stuck video {video.id} (Status: {video.status}, Created: {video.created_at})")

            # 1. Помечаем как ошибку
            video.status = 'error'
            video.save()

            # 2. Пытаемся удалить временный исходник, если он остался (хотя путь мы могли потерять)
            # Если бы мы хранили путь к temp файлу в БД, мы бы его удалили тут.
            # Но обычно ОС или Docker сами чистят /tmp при перезагрузке, или мы полагаемся на очистку при старте.

            affected_rooms.add(str(video.room.uuid))
            count += 1

        except Exception as e:
            logger.error(f"Error fixing stuck video {video.id}: {e}")

        # Удаляем папки транскодинга старше таймаута
        transcode_root = os.path.join(AppConfig.BASE_DIR, "storage", "temp_transcode")
        if os.path.exists(transcode_root):
            for dirname in os.listdir(transcode_root):
                dirpath = os.path.join(transcode_root, dirname)
                if os.path.isdir(dirpath):
                    # Проверяем время модификации папки
                    try:
                        mtime = datetime.fromtimestamp(os.path.getmtime(dirpath))
                        if mtime < cutoff_time:
                            logger.info(f"Removing stale transcode dir: {dirname}")
                            shutil.rmtree(dirpath, ignore_errors=True)
                    except Exception as e:
                        logger.error(f"Error cleaning temp dir {dirname}: {e}")

    # Уведомляем комнаты, чтобы у пользователей пропала вечная загрузка
    for r_uuid in affected_rooms:
        try:
            send_refresh_to_flask(r_uuid)
        except:
            pass

    if count > 0:
        logger.info(f"Fixed {count} stuck videos.")

