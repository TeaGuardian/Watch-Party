#/core/storage.py
import os
import shutil
import io
import logging
from abc import ABC, abstractmethod
from minio import Minio
from minio.error import S3Error

# Импортируем конфиг
# Обрати внимание: импорт идет из корневого config, так как core лежит внутри
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import StorageConfig, AppConfig

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("StorageManager")


class BaseStorageManager(ABC):
    """Абстрактный класс для унификации методов хранения"""

    @abstractmethod
    def save_file(self, file_bytes: bytes, path: str, content_type: str = None) -> bool:
        """Сохранить файл. path включает имя файла и структуру папок"""
        pass

    @abstractmethod
    def get_file(self, path: str) -> bytes | None:
        """Получить содержимое файла"""
        pass

    @abstractmethod
    def delete_file(self, path: str) -> bool:
        """Удалить конкретный файл"""
        pass

    @abstractmethod
    def delete_folder(self, folder_path: str) -> bool:
        """Рекурсивное удаление папки (или объектов с префиксом)"""
        pass

    @abstractmethod
    def get_url(self, path: str) -> str:
        """Получить ссылку на файл (для frontend)"""
        pass


class LocalStorageManager(BaseStorageManager):
    """Менеджер для локального хранения (Dev Mode)"""

    def __init__(self):
        self.root_path = AppConfig.LOCAL_STORAGE_PATH
        logger.info(f"Initialized Local Storage at: {self.root_path}")

    def _full_path(self, path: str) -> str:
        # Защита от выхода за пределы папки (хотя бы базовая)
        return os.path.join(self.root_path, path.strip("/"))

    def save_file(self, file_bytes: bytes, path: str, content_type: str = None) -> bool:
        try:
            full_path = self._full_path(path)
            # Создаем структуру папок (user_id/room_uuid/...)
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
        """Удаляет папку рекурсивно"""
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
        # В локальном режиме файлы раздаем через Flask-route (например, /content/...)
        # Здесь возвращаем относительный путь, который подставится в src
        return f"/content/{path}"


class MinioStorageManager(BaseStorageManager):
    """Менеджер для MinIO / S3 (Prod Mode)"""

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
            # path в S3 работает как ключ: 'folder/subfolder/file.ext'
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
        В S3 нет папок. Удаляем все объекты, начинающиеся с префикса.
        folder_path должен заканчиваться на '/', например 'user_1/'
        """
        if not folder_path.endswith('/'):
            folder_path += '/'

        try:
            # list_objects(recursive=True) найдет все файлы внутри "папки"
            objects_to_delete = self.client.list_objects(
                self.bucket, prefix=folder_path, recursive=True
            )

            # Собираем ошибки удаления (DeleteObjectOnError)
            errors = map(
                lambda x: x.object_name,
                self.client.remove_objects(self.bucket, objects_to_delete)
            )

            has_error = False
            for error in errors:
                logger.error(f"Error deleting object {error}")
                has_error = True

            if not has_error:
                logger.info(f"S3 'folder' cleaned: {folder_path}")
                return True
            return False
        except Exception as e:
            logger.error(f"S3 recursive delete error ({folder_path}): {e}")
            return False

    def get_url(self, path: str) -> str:
        # Для продакшена обычно MinIO настраивают так,
        # чтобы бакет был публичным для чтения (Read Only), или через Nginx.
        # Здесь мы предполагаем прямой доступ: http://minio:9000/bucket/path
        protocol = "https" if StorageConfig.SECURE else "http"
        return f"{protocol}://{StorageConfig.ENDPOINT}/{self.bucket}/{path}"


# --- Инициализация Singleton-объекта ---

if StorageConfig.USE_S3:
    storage = MinioStorageManager()
else:
    storage = LocalStorageManager()

# Экспортируем объект storage, чтобы импортировать его так:
# from core.storage import storage
# storage.save_file(...)