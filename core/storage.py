# /core/storage.py
import os
import shutil
import io
import logging
from abc import ABC, abstractmethod
from minio import Minio
from minio.error import S3Error
from datetime import datetime, timezone
from minio.deleteobjects import DeleteObject

import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import StorageConfig, AppConfig

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("StorageManager")


class BaseStorageManager(ABC):
    @abstractmethod
    def save_file(self, file_bytes: bytes, path: str, content_type: str = None) -> bool:
        pass

    @abstractmethod
    def get_file(self, path: str) -> bytes | None:
        pass

    @abstractmethod
    def delete_file(self, path: str) -> bool:
        pass

    @abstractmethod
    def delete_folder(self, folder_path: str) -> bool:
        pass

    @abstractmethod
    def get_url(self, path: str) -> str:
        pass

    @abstractmethod
    def list_subfolders(self, prefix: str) -> list[str]:
        """Возвращает список имен подпапок в указанном пути"""
        pass

    @abstractmethod
    def get_folder_last_modified(self, folder_path: str) -> datetime | None:
        """Возвращает дату модификации самого свежего файла в папке"""
        pass


class LocalStorageManager(BaseStorageManager):
    def __init__(self):
        self.root_path = AppConfig.LOCAL_STORAGE_PATH
        logger.info(f"Initialized Local Storage at: {self.root_path}")

    def _full_path(self, path: str) -> str:
        return os.path.join(self.root_path, path.strip("/"))

    def save_file(self, file_bytes: bytes, path: str, content_type: str = None) -> bool:
        try:
            full_path = self._full_path(path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, 'wb') as f:
                f.write(file_bytes)
            logger.info(f"Local saved: {path}")
            return True
        except Exception as e:
            logger.error(f"Local save error ({path}): {e}")
            return False

    def get_file(self, path: str) -> bytes | None:
        try:
            full_path = self._full_path(path)
            if not os.path.exists(full_path):
                return None
            with open(full_path, 'rb') as f:
                return f.read()
        except Exception as e:
            logger.error(f"Local read error ({path}): {e}")
            return None

    def delete_file(self, path: str) -> bool:
        try:
            full_path = self._full_path(path)
            if os.path.exists(full_path):
                os.remove(full_path)
                logger.info(f"Local deleted: {path}")
                return True
            return False
        except Exception as e:
            logger.error(f"Local delete error ({path}): {e}")
            return False

    def delete_folder(self, folder_path: str) -> bool:
        try:
            full_path = self._full_path(folder_path)
            if os.path.exists(full_path):
                shutil.rmtree(full_path)
                logger.info(f"Local folder deleted: {folder_path}")
                return True
            return False
        except Exception as e:
            logger.error(f"Local folder delete error ({folder_path}): {e}")
            return False

    def get_url(self, path: str) -> str:
        return f"/content/{path}"

    def list_subfolders(self, prefix: str) -> list[str]:
        full_path = self._full_path(prefix)
        if not os.path.exists(full_path):
            return []
        # Получаем только директории
        return [
            name for name in os.listdir(full_path)
            if os.path.isdir(os.path.join(full_path, name))
        ]

    def get_folder_last_modified(self, folder_path: str) -> datetime | None:
        full_path = self._full_path(folder_path)
        if not os.path.exists(full_path):
            return None

        latest_time = 0.0
        # Рекурсивно ищем самый свежий файл
        for root, _, files in os.walk(full_path):
            for file in files:
                mtime = os.path.getmtime(os.path.join(root, file))
                if mtime > latest_time:
                    latest_time = mtime

        if latest_time == 0.0:
            return None

        return datetime.fromtimestamp(latest_time, tz=timezone.utc)


class MinioStorageManager(BaseStorageManager):
    def __init__(self):
        self.bucket = StorageConfig.BUCKET_NAME
        self.client = Minio(
            StorageConfig.ENDPOINT,
            access_key=StorageConfig.ACCESS_KEY,
            secret_key=StorageConfig.SECRET_KEY,
            secure=StorageConfig.SECURE
        )
        self._ensure_bucket()
        logger.info(f"Initialized MinIO Storage. Bucket: {self.bucket}")

    def _ensure_bucket(self):
        try:
            if not self.client.bucket_exists(self.bucket):
                self.client.make_bucket(self.bucket)
        except Exception as e:
            logger.critical(f"MinIO bucket check failed: {e}")

    def save_file(self, file_bytes: bytes, path: str, content_type: str = None) -> bool:
        try:
            file_stream = io.BytesIO(file_bytes)
            self.client.put_object(
                self.bucket,
                path,
                file_stream,
                length=len(file_bytes),
                content_type=content_type or "application/octet-stream"
            )
            logger.info(f"S3 saved: {path}")
            return True
        except S3Error as e:
            logger.error(f"S3 save error ({path}): {e}")
            return False

    def get_file(self, path: str) -> bytes | None:
        response = None
        try:
            response = self.client.get_object(self.bucket, path)
            return response.read()
        except S3Error as e:
            logger.error(f"S3 read error ({path}): {e}")
            return None
        finally:
            if response:
                response.close()

    def delete_file(self, path: str) -> bool:
        try:
            self.client.remove_object(self.bucket, path)
            logger.info(f"S3 deleted: {path}")
            return True
        except S3Error as e:
            logger.error(f"S3 delete error ({path}): {e}")
            return False

    def delete_folder(self, folder_path: str) -> bool:
        """
        Удаление папки в S3.
        Используем простой цикл remove_object вместо remove_objects,
        чтобы избежать проблем с сериализацией XML (ошибка toxml).
        """
        if not folder_path.endswith('/'):
            folder_path += '/'

        try:
            # 1. Получаем список всех объектов в папке
            objects_iter = self.client.list_objects(
                self.bucket, prefix=folder_path, recursive=True
            )

            count = 0
            # 2. Удаляем каждый объект по отдельности
            for obj in objects_iter:
                try:
                    self.client.remove_object(self.bucket, obj.object_name)
                    count += 1
                except Exception as inner_e:
                    logger.error(f"Failed to delete individual object {obj.object_name}: {inner_e}")

            if count > 0:
                logger.info(f"S3 'folder' cleaned: {folder_path} ({count} files)")
            else:
                logger.info(f"S3 folder empty or not found: {folder_path}")

            return True

        except Exception as e:
            logger.error(f"S3 delete_folder error ({folder_path}): {e}")
            return False

    def get_url(self, path: str) -> str:
        """
        Возвращает относительный URL, который будет обработан Nginx.
        Nginx перенаправит /s3/... -> MinIO container
        """
        return f"/s3/{self.bucket}/{path}"

    def list_subfolders(self, prefix: str) -> list[str]:
        if not prefix.endswith('/'):
            prefix += '/'

        results = []
        try:
            # recursive=False заставляет S3 группировать файлы по префиксам (папкам)
            objects = self.client.list_objects(self.bucket, prefix=prefix, recursive=False)
            for obj in objects:
                if obj.is_dir:
                    # object_name вернет "shared_content/hash_123/"
                    # Нам нужно вычленить "hash_123"
                    # Убираем префикс родителя и слеш в конце
                    clean_name = obj.object_name[len(prefix):].strip('/')
                    results.append(clean_name)
        except Exception as e:
            logger.error(f"S3 list_subfolders error: {e}")

        return results

    def get_folder_last_modified(self, folder_path: str) -> datetime | None:
        if not folder_path.endswith('/'):
            folder_path += '/'

        try:
            # Берем список файлов, чтобы найти самый свежий
            # (Можно ограничить limit, если папки огромные, но для точности лучше проверить все)
            objects = self.client.list_objects(self.bucket, prefix=folder_path, recursive=True)

            last_modified = None

            for obj in objects:
                if not last_modified or obj.last_modified > last_modified:
                    last_modified = obj.last_modified

            return last_modified
        except Exception as e:
            logger.error(f"S3 get_last_modified error: {e}")
            return None


if StorageConfig.USE_S3:
    storage = MinioStorageManager()
else:
    storage = LocalStorageManager()