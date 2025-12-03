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
    """
    Конвертирует видео в HLS (m3u8) и загружает в хранилище.
    """
    logger.info(f"Start processing video {video_id} from {local_source_path}")

    # 1. Получаем видео из БД
    try:
        video = Video.get_by_id(video_id)
    except Exception:
        logger.error(f"Video {video_id} not found inside task")
        return

    video.status = 'processing'
    video.save()

    # Папки
    # Уникальная временная папка для нарезки сегментов
    transcode_dir = os.path.join(AppConfig.BASE_DIR, "storage", "temp_transcode", str(uuid4()))
    os.makedirs(transcode_dir, exist_ok=True)

    # Имя выходного плейлиста
    playlist_name = "index.m3u8"
    output_path = os.path.join(transcode_dir, playlist_name)

    try:
        # 2. Запуск FFmpeg
        command = [
            'ffmpeg', '-y', '-i', local_source_path,
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',  # Видео кодек
            '-c:a', 'aac', '-b:a', '128k',  # Аудио кодек
            '-hls_time', '6',
            '-hls_playlist_type', 'vod',
            '-hls_segment_filename', os.path.join(transcode_dir, 'segment_%03d.ts'),
            output_path
        ]

        logger.info(f"Running ffmpeg: {' '.join(command)}")

        # Запускаем процесс и ждем завершения
        process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        if process.returncode != 0:
            raise Exception(f"FFmpeg failed: {process.stderr.decode()}")

        # 3. Загрузка результатов в Storage
        # Структура: user_{id}/room_{uuid}/video_{id}/...
        room: Room = video.room
        user: User = room.owner

        # Путь в хранилище (префикс папки)
        storage_folder = f"users/{user.id}/rooms/{room.uuid}/videos/{video.id}/"

        # Проходим по всем созданным файлам (.m3u8 и .ts)
        for filename in os.listdir(transcode_dir):
            file_path = os.path.join(transcode_dir, filename)
            if os.path.isfile(file_path):
                with open(file_path, 'rb') as f:
                    content = f.read()

                # Сохраняем (LocalStorage или MinIO - неважно, метод один)
                dest_path = storage_folder + filename
                success = storage.save_file(content, dest_path)

                if not success:
                    raise Exception(f"Failed to upload segment {filename}")

        # 4. Обновляем статус в БД
        video.status = 'ready'
        # Ссылка на плейлист (относительный путь для storage.get_url)
        full_path = storage_folder + playlist_name
        video.storage_path = full_path.replace('\\', '/')

        video.save()
        logger.info(f"Video {video_id} processed successfully.")

    except Exception as e:
        logger.error(f"Processing failed: {e}")
        video.status = 'error'
        video.save()

    finally:
        # 5. Очистка временных файлов
        if os.path.exists(transcode_dir):
            shutil.rmtree(transcode_dir)

        if os.path.exists(local_source_path):
            os.remove(local_source_path)


@app.task(name='tasks.media.delete_storage_folder')
def delete_storage_folder_task(path: str):
    if not path: return
    logger.info(f"Deleting storage folder: {path}")
    success = storage.delete_folder(path)
    if success:
        logger.info(f"Successfully deleted: {path}")
    else:
        logger.warning(f"Failed to delete (or not found): {path}")


@app.task(name='tasks.media.delete_account_files')
def delete_account_files_task(user_id: int):
    logger.info(f"Deleting files for user {user_id}")
    folder_prefix = f"users/{user_id}/"
    success = storage.delete_folder(folder_prefix)
    if success:
        logger.info(f"Successfully deleted files for user {user_id}")
    else:
        logger.error(f"Failed to delete files for user {user_id}")


@app.task(name='tasks.media.cleanup_old_videos')
def cleanup_old_videos_task():
    """
    Удаляет видео, которые не использовались более 2 часов.
    Пропускаем видео, которые еще в обработке или загрузке.
    """
    logger.info("Starting cleanup of old videos...")

    cutoff_time = datetime.now() - timedelta(hours=2)

    # Ищем видео, где last_played_at старее 2 часов и статус 'ready'
    # Также можно проверять created_at, чтобы не удалять только что загруженные,
    # но last_played_at инициализируется now(), так что свежие видео не попадут.
    old_videos = Video.select().where(
        (Video.last_played_at < cutoff_time) &
        (Video.status == 'ready')
    )

    deleted_count = 0
    for video in old_videos:
        try:
            logger.info(f"Cleaning up inactive video {video.id} (last played: {video.last_played_at})")

            # Удаляем файлы
            owner_id = video.room.owner_id
            room_uuid = video.room.uuid
            video_folder = f"users/{owner_id}/rooms/{room_uuid}/videos/{video.id}/"

            # Синхронное удаление, так как мы уже внутри celery задачи
            storage.delete_folder(video_folder)

            # Удаляем запись
            video.delete_instance()
            deleted_count += 1

        except Exception as e:
            logger.error(f"Error cleaning video {video.id}: {e}")

    logger.info(f"Cleanup finished. Deleted {deleted_count} videos.")