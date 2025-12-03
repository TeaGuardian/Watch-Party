# /tasks/media.py
import os
import shutil
import subprocess
import logging
from uuid import uuid4
from datetime import datetime, timedelta

from tasks.celery_app import app
from core.models import Video, Room, User
from core.storage import storage
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import AppConfig

logger = logging.getLogger("CeleryMedia")


@app.task(name='tasks.media.process_video')
def process_video_task(video_id: int, local_source_path: str):
    logger.info(f"Start processing video {video_id} from {local_source_path}")

    # [NEW] Проверка 1: А не удалил ли пользователь видео, пока оно стояло в очереди?
    try:
        video = Video.get_by_id(video_id)
    except Exception:
        logger.info(f"Video {video_id} record missing. Cancelling processing (User deleted it?).")
        if os.path.exists(local_source_path):
            os.remove(local_source_path)
        return

    video.status = 'processing'
    video.save()

    transcode_dir = os.path.join(AppConfig.BASE_DIR, "storage", "temp_transcode", str(uuid4()))
    os.makedirs(transcode_dir, exist_ok=True)
    playlist_name = "index.m3u8"
    output_path = os.path.join(transcode_dir, playlist_name)

    try:
        # [NEW] Проверка 2: Перед тяжелым FFmpeg еще раз проверим (если очередь была долгой)
        if not Video.select().where(Video.id == video_id).exists():
            raise Exception("Video deleted by user before transcoding")

        command = [
            'ffmpeg', '-y', '-i', local_source_path,
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
            '-c:a', 'aac', '-b:a', '128k',
            '-hls_time', '6',
            '-hls_playlist_type', 'vod',
            '-hls_segment_filename', os.path.join(transcode_dir, 'segment_%03d.ts'),
            output_path
        ]

        # Получаем длительность через ffprobe (опционально, но полезно)
        # Здесь опустим для краткости, оставим 0 или старую логику

        process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if process.returncode != 0:
            raise Exception(f"FFmpeg failed: {process.stderr.decode()}")

        # [NEW] Проверка 3: Пользователь мог удалить видео ВО ВРЕМЯ обработки
        if not Video.select().where(Video.id == video_id).exists():
            raise Exception("Video deleted by user during transcoding")

        # 3. Загрузка.
        # [NEW] Используем путь на основе ХЭША, а не ID видео
        # Если хэша нет (старое видео или баг), фолбэк на старую логику, но у нас он есть.
        if video.file_hash:
            storage_folder = f"{AppConfig.SHARED_CONTENT_PATH}/{video.file_hash}/"
        else:
            # Fallback (не должно случаться при новом коде)
            storage_folder = f"users/{video.room.owner_id}/rooms/{video.room.uuid}/videos/{video.id}/"

        for filename in os.listdir(transcode_dir):
            file_path = os.path.join(transcode_dir, filename)
            if os.path.isfile(file_path):
                with open(file_path, 'rb') as f:
                    content = f.read()
                dest_path = storage_folder + filename
                if not storage.save_file(content, dest_path):
                    raise Exception(f"Failed to upload {filename}")

        # 4. Финиш
        # Еще раз перечитываем запись, чтобы не перезатереть возможные изменения (мало ли)
        # Но peewee объекты не обновляются сами.
        video = Video.get_by_id(video_id)

        video.status = 'ready'
        full_path = storage_folder + playlist_name
        video.storage_path = full_path.replace('\\', '/')

        # Попытка достать duration из метаданных (упрощенно - размер сегментов * кол-во)
        # Или просто оставим как есть.

        video.save()
        logger.info(f"Video {video_id} processed successfully. Stored at {storage_folder}")

    except Exception as e:
        logger.error(f"Processing interrupted/failed: {e}")
        # Если ошибка "Video deleted...", то запись в БД уже нет, save() упадет.
        # Проверяем существование перед обновлением статуса
        try:
            v = Video.get_or_none(Video.id == video_id)
            if v:
                v.status = 'error'
                v.save()
        except:
            pass

    finally:
        if os.path.exists(transcode_dir):
            shutil.rmtree(transcode_dir)
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
    cutoff_time = datetime.now() - timedelta(hours=1)

    old_videos = Video.select().where(
        (Video.last_played_at < cutoff_time) &
        (Video.status == 'ready')
    )

    deleted_records = 0
    deleted_files = 0

    for video in old_videos:
        try:
            file_hash = video.file_hash
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

        except Exception as e:
            logger.error(f"Error cleaning video {video.id}: {e}")

    logger.info(f"Cleanup finished. Records removed: {deleted_records}. File groups removed: {deleted_files}.")