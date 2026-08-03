"""
Примитивы информационной безопасности КТК ЭЛОУ-АВТ.

Закрывает критерий «информационная безопасность»:
  • хеширование паролей — Argon2id (победитель Password Hashing Competition,
    лицензия MIT, патентно чист). SHA-256 из учебной заглушки больше не
    применяется: старые хеши распознаются и молча переводятся в Argon2id
    при первом успешном входе, сбрасывать пароли не нужно;
  • сеанс — JWT (HS256) в cookie httpOnly + SameSite=Strict. Cookie выбран
    осознанно: браузерный WebSocket не умеет передавать заголовок
    Authorization, а cookie уходит и в REST-запросе, и в WS-рукопожатии;
  • защита от подбора пароля — счётчик неудачных попыток с временной
    блокировкой учётной записи.

Секрет подписи задаётся переменной окружения KTK_SECRET_KEY. Если её нет,
используется отладочный ключ и печатается предупреждение — на стенде
это допустимо, в промышленной эксплуатации ключ обязателен.
"""
from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# ------------------------------------------------------------------ настройки

#: Имя cookie с токеном сеанса.
COOKIE_NAME = "ktk_session"

#: Срок жизни сеанса — одна рабочая смена.
SESSION_TTL_HOURS = int(os.getenv("KTK_SESSION_TTL_HOURS", "8"))

#: Отправлять cookie только по HTTPS. На демо-стенде по http выключено.
COOKIE_SECURE = os.getenv("KTK_COOKIE_SECURE", "0") == "1"

_DEV_SECRET = "ktk-eloyavt-dev-secret-not-for-production"
SECRET_KEY = os.getenv("KTK_SECRET_KEY") or _DEV_SECRET
ALGORITHM = "HS256"

if SECRET_KEY == _DEV_SECRET:
    print("[security] ВНИМАНИЕ: KTK_SECRET_KEY не задан, используется отладочный "
          "ключ. Для промышленной эксплуатации задайте переменную окружения.")

#: Порог блокировки: N неудачных попыток за окно WINDOW → блокировка на LOCK.
MAX_FAILED_ATTEMPTS = int(os.getenv("KTK_MAX_FAILED_ATTEMPTS", "5"))
FAILED_WINDOW_S = int(os.getenv("KTK_FAILED_WINDOW_S", "900"))    # 15 минут
LOCKOUT_S = int(os.getenv("KTK_LOCKOUT_S", "300"))                # 5 минут

#: Минимальная длина пароля, назначаемого через администрирование.
#: Учебные учётные записи из `seed.py` короче намеренно (пароль равен логину,
#: так удобнее на демонстрации) — политика распространяется на пароли, которые
#: заводит администратор, а демо-записи README предписывает менять на стенде.
MIN_PASSWORD_LENGTH = int(os.getenv("KTK_MIN_PASSWORD_LENGTH", "8"))

_hasher = PasswordHasher()


# ------------------------------------------------------------------- пароли

def hash_password(password: str) -> str:
    """Хеш пароля Argon2id. Хеш самоописателен: '$argon2id$v=19$m=...'."""
    return _hasher.hash(password)


def is_legacy_hash(stored: str) -> bool:
    """Старый хеш из учебной заглушки — «голый» SHA-256 (64 hex-символа)."""
    return len(stored) == 64 and all(c in "0123456789abcdef" for c in stored.lower())


def verify_password(stored: str, password: str) -> bool:
    """
    Проверить пароль. Понимает и Argon2id, и устаревший SHA-256 —
    чтобы уже созданные базы продолжали работать без сброса паролей.
    """
    if not stored:
        return False
    if is_legacy_hash(stored):
        return hashlib.sha256(password.encode()).hexdigest() == stored.lower()
    try:
        return _hasher.verify(stored, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored: str) -> bool:
    """Нужно ли перехешировать: старый алгоритм или устаревшие параметры Argon2."""
    if is_legacy_hash(stored):
        return True
    try:
        return _hasher.check_needs_rehash(stored)
    except InvalidHashError:
        return True


# -------------------------------------------------------------- токен сеанса

def create_token(user_id: int, login: str, role: str) -> str:
    """Подписанный JWT сеанса. Внутрь кладём только то, что нужно для доступа."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "login": login,
        "role": role,
        "iat": now,
        "exp": now + timedelta(hours=SESSION_TTL_HOURS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict | None:
    """Разобрать и проверить токен. None — если подпись неверна или срок истёк."""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


# ------------------------------------------------- защита от подбора пароля

#: login -> (время последней неудачи, счётчик неудач подряд)
_failed: dict[str, tuple[float, int]] = {}


def lockout_remaining(login: str) -> int:
    """Сколько секунд осталось до разблокировки. 0 — учётная запись не заблокирована."""
    rec = _failed.get(login)
    if not rec:
        return 0
    last, count = rec
    if count < MAX_FAILED_ATTEMPTS:
        return 0
    left = int(LOCKOUT_S - (time.time() - last))
    return max(0, left)


def register_failure(login: str) -> int:
    """Учесть неудачную попытку входа. Возвращает число неудач подряд."""
    now = time.time()
    last, count = _failed.get(login, (0.0, 0))
    # серия обнуляется, если предыдущая неудача была давно
    count = count + 1 if now - last <= FAILED_WINDOW_S else 1
    _failed[login] = (now, count)
    return count


def register_success(login: str) -> None:
    """Успешный вход обнуляет счётчик неудач."""
    _failed.pop(login, None)


def reset_failures() -> None:
    """Полный сброс счётчиков — используется в автотестах."""
    _failed.clear()
