#/app/decorators.py
from functools import wraps
from flask import session, jsonify, redirect, Response
from core.models import User


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # 1. Проверяем наличие ID в сессии
        user_id = session.get('user_id')
        if not user_id:
            return jsonify({'error': 'Unauthorized', 'code': 'auth_required'}), 401

        # 2. Получаем пользователя из БД
        user = User.get_or_none(User.id == user_id)
        if not user:
            session.clear()
            return jsonify({'error': 'User not found', 'code': 'auth_required'}), 401

        # 3. Проверка на бан
        if user.status == 'banned':
            session.clear()
            return jsonify({'error': 'Account is banned', 'code': 'banned'}), 403

        # Прокидываем объект пользователя в функцию-контроллер
        return f(current_user=user, *args, **kwargs)

    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(current_user: User, *args, **kwargs):
        # Декоратор ставится ПОСЛЕ @login_required, поэтому current_user уже есть
        if current_user.role != 'admin':
            return jsonify({'error': 'Admin access required'}), 403
        return f(current_user=current_user, *args, **kwargs)

    return decorated_function