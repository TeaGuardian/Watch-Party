#/core/models.py
import os
import sys
import uuid
from datetime import datetime
from peewee import (
    Model, CharField, BooleanField, DateTimeField,
    ForeignKeyField, IntegerField, BigIntegerField,
    UUIDField, TextField
)
from werkzeug.security import generate_password_hash, check_password_hash
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DBConfig
from core.database import db


class BaseModel(Model):
    """Базовая модель, привязанная к нашей БД"""

    class Meta:
        database = db


# --- Пользователи ---

class User(BaseModel):
    # Основные данные
    username = CharField(unique=True, index=True)
    password_hash = CharField()

    # Роли: 'user', 'moderator', 'admin'
    role = CharField(default='user')

    # Статусы: 'new', 'tg_verified', 'approved', 'banned'
    status = CharField(default='new')

    # Telegram интеграция
    tg_id = BigIntegerField(null=True, unique=True)
    tg_username = CharField(null=True)
    verification_code = CharField(null=True)  # Код для старта бота
    avatar_path = CharField(null=True)

    # Кто подтвердил аккаунт (для админов/модераторов)
    approved_by = ForeignKeyField('self', null=True, backref='approved_users')

    # Технические поля
    cookie_consent = BooleanField(default=False)
    created_at = DateTimeField(default=datetime.now)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == 'admin'

    @property
    def is_moderator(self):
        return self.role in ['admin', 'moderator']

    @property
    def is_active(self):
        """Разрешен ли вход и действия"""
        return self.status == 'approved' or self.role == 'admin'

    def to_dict(self):
        from core.storage import storage
        if self.avatar_path:
            url = storage.get_url(self.avatar_path)
        else:
            url = f"https://ui-avatars.com/api/?name={self.username}&background=random&size=200"
        return {
            'id': self.id,
            'username': self.username,
            'role': self.role,
            'status': self.status,
            'tg_connected': bool(self.tg_id),
            'tg_username': self.tg_username,
            'avatar_url': url,
            'has_custom_avatar': bool(self.avatar_path)
        }


# --- Комнаты ---

class Room(BaseModel):
    uuid = UUIDField(default=uuid.uuid4, unique=True, index=True)

    # При удалении юзера - удаляем комнату
    owner = ForeignKeyField(User, backref='rooms', on_delete='CASCADE')

    name = CharField()
    header_color = CharField(default='#0d6efd')  # Bootstrap Primary Blue

    # Приватность
    is_private = BooleanField(default=False)
    # Разрешить зрителям управлять плеером (только для закрытых)
    allow_guest_control = BooleanField(default=False)

    created_at = DateTimeField(default=datetime.now)

    def to_dict(self):
        return {
            'uuid': str(self.uuid),
            'name': self.name,
            'owner_id': self.owner.id,
            'owner_name': self.owner.username,
            'header_color': self.header_color,
            'is_private': self.is_private,
            'allow_guest_control': self.allow_guest_control,
            'created_at': self.created_at.isoformat()
        }


class RoomBan(BaseModel):
    """Таблица банов внутри конкретной комнаты (опционально)"""
    room = ForeignKeyField(Room, backref='bans', on_delete='CASCADE')
    user = ForeignKeyField(User, backref='room_bans', on_delete='CASCADE')
    created_at = DateTimeField(default=datetime.now)


# --- Контент (Видео) ---

class Video(BaseModel):
    room = ForeignKeyField(Room, backref='videos', on_delete='CASCADE')
    title = CharField()
    storage_path = CharField()
    status = CharField(default='uploading')
    duration = IntegerField(default=0)  # В секундах
    created_at = DateTimeField(default=datetime.now)
    order = IntegerField(default=0)

    # Новые поля для контроля хранилища
    file_hash = CharField(null=True, index=True) # MD5 хеш для дедупликации
    file_size = BigIntegerField(default=0)       # Размер в байтах
    last_played_at = DateTimeField(default=datetime.now) # Для автоудаления

    def to_dict(self):
        from core.storage import storage
        url = storage.get_url(self.storage_path) if self.status == 'ready' else None

        return {
            'id': self.id,
            'title': self.title,
            'status': self.status,
            'duration': self.duration,
            'url': url,
            'created_at': self.created_at.isoformat(),
            'size_mb': round(self.file_size / (1024*1024), 1)
        }


# --- Новости ---

class NewsPost(BaseModel):
    author = ForeignKeyField(User, backref='posts')
    content = TextField()  # Markdown текст
    created_at = DateTimeField(default=datetime.now)

    def to_dict(self):
        return {
            'id': self.id,
            'author': self.author.username,
            'content': self.content,
            'created_at': self.created_at.strftime("%Y-%m-%d %H:%M")
        }


class RoomAccess(BaseModel):
    """Таблица прав доступа в закрытые комнаты"""
    room = ForeignKeyField(Room, backref='allowed_users', on_delete='CASCADE')
    user = ForeignKeyField(User, backref='allowed_rooms', on_delete='CASCADE')
    created_at = DateTimeField(default=datetime.now)

    class Meta:
        # Уникальная пара: один юзер в одной комнате один раз
        indexes = (
            (('room', 'user'), True),
        )


# --- Функция инициализации таблиц ---

def create_tables():
    with db:
        # Добавлен параметр safe=True, чтобы не падать, если таблицы уже есть
        db.create_tables([User, Room, RoomBan, Video, NewsPost, RoomAccess], safe=True)