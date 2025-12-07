# /tasks/media.py
import os
import shutil
import subprocess
import logging
from uuid import uuid4
from datetime import datetime, timedelta

# [UPD] Импортируем SocketIO из пакета, а НЕ из app
from flask_socketio import SocketIO

from tasks.celery_app import app
from core.models import Video, Room, User
from core.storage import storage
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import AppConfig, RedisConfig

logger = logging.getLogger("CeleryMedia")

# [UPD] Создаем независимый эмиттер, подключенный к тому же Redis
# message_queue должен совпадать с тем, что в app/__init__.py
celery_socketio = SocketIO(message_queue=RedisConfig.URL)


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
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return float(result.stdout)
    except Exception as e:
        logger.error(f"FFprobe error: {e}")
        return 0.0


@app.task(name='tasks.media.process_video')
def process_video_task(video_id: int, local_source_path: str):
    logger.info(f"Start processing video {video_id}")

    try:
        video = Video.get_by_id(video_id)
    except Exception:
        if os.path.exists(local_source_path): os.remove(local_source_path)
        return

    video.status = 'processing'
    video.save()

    room_uuid = str(video.room.uuid)

    transcode_dir = os.path.join(AppConfig.BASE_DIR, "storage", "temp_transcode", str(uuid4()))
    os.makedirs(transcode_dir, exist_ok=True)
    playlist_name = "index.m3u8"
    output_path = os.path.join(transcode_dir, playlist_name)

    try:
        if not Video.select().where(Video.id == video_id).exists():
            raise Exception("Deleted before start")

        total_duration = get_video_duration(local_source_path)
        video.duration = int(total_duration)
        video.save()

        command = [
            'ffmpeg', '-y', '-i', local_source_path,
            '-threads', '0',
            '-c:v', 'libx264',
            '-preset', 'veryfast',
            '-crf', '24',
            '-c:a', 'aac', '-b:a', '128k',
            '-hls_time', '6',
            '-hls_playlist_type', 'vod',
            '-hls_segment_filename', os.path.join(transcode_dir, 'segment_%03d.ts'),
            '-progress', 'pipe:1',
            output_path
        ]

        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        last_percent = -1

        for line in process.stdout:
            if "out_time_ms=" in line:
                try:
                    time_ms = int(line.split('=')[1].strip())
                    current_seconds = time_ms / 1000000.0

                    if total_duration > 0:
                        percent = int((current_seconds / total_duration) * 100)
                        percent = min(99, max(0, percent))

                        if percent > last_percent:
                            last_percent = percent
                            celery_socketio.emit('processing_progress', {
                                'video_id': video_id,
                                'percent': percent
                            }, room=room_uuid)

                except ValueError:
                    pass

        process.wait()
        if process.returncode != 0:
            raise Exception("FFmpeg process failed")

        if not Video.select().where(Video.id == video_id).exists():
            raise Exception("Video deleted during transcoding")

        if video.file_hash:
            storage_folder = f"{AppConfig.SHARED_CONTENT_PATH}/{video.file_hash}/"
        else:
            storage_folder = f"users/{video.room.owner_id}/rooms/{video.room.uuid}/videos/{video.id}/"

        for filename in os.listdir(transcode_dir):
            file_path = os.path.join(transcode_dir, filename)
            if os.path.isfile(file_path):
                with open(file_path, 'rb') as f:
                    content = f.read()
                dest_path = storage_folder + filename
                if not storage.save_file(content, dest_path):
                    raise Exception(f"Failed to upload {filename}")

        video = Video.get_by_id(video_id)
        video.status = 'ready'
        video.storage_path = (storage_folder + playlist_name).replace('\\', '/')
        video.save()

        celery_socketio.emit('playlist_refresh', {}, room=room_uuid)
        logger.info(f"Video {video_id} done.")

    except Exception as e:
        logger.error(f"Processing failed: {e}")
        try:
            v = Video.get_or_none(Video.id == video_id)
            if v:
                v.status = 'error'
                v.save()
                celery_socketio.emit('playlist_refresh', {}, room=room_uuid)
        except:
            pass

    finally:
        if os.path.exists(transcode_dir): shutil.rmtree(transcode_dir)
        if os.path.exists(local_source_path): os.remove(local_source_path)


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
            celery_socketio.emit('playlist_refresh', {}, to=r_uuid)
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
            celery_socketio.emit('playlist_refresh', {}, room=r_uuid)
        except:
            pass

    if count > 0:
        logger.info(f"Fixed {count} stuck videos.")

