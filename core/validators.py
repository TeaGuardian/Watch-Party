#/core/validators.py
import re


def validate_password_strength(password: str) -> tuple[bool, str]:
    """
    Возвращает (bool, str): (прошел ли проверку, сообщение об ошибке)
    """
    strength = 0
    if len(password) >= 8: strength += 1
    if len(password) >= 12: strength += 1
    if re.search(r'\d', password): strength += 1
    if re.search(r'[!@#$%^&*(),.?":{}|<>]', password): strength += 1
    if re.search(r'[a-z]', password) and re.search(r'[A-Z]', password): strength += 1

    # Минимум 3 балла
    if strength < 3:
        return False, "Пароль слишком слабый (нужны цифры, разный регистр, 8+ символов)"
    return True, ""