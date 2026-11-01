"""Пароли (argon2id), токены сессий, шифрование хранимых секретов (Fernet)."""
import hashlib
import hmac
import secrets
import threading

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken

from app.config import config

# PasswordHasher по умолчанию использует argon2id
_hasher = PasswordHasher()
# Хеш для сравнения, когда пользователя нет: время ответа не выдаёт, существует ли логин
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing")

MIN_PASSWORD_LEN = 8
# Каждая проверка argon2 берёт ~64 МБ: без ограничения наплыв запросов на /login съест память Pi (аудит A-02).
# Лишние запросы ждут в очереди, а не отказывают — обычный вход это не замечает.
ARGON2_SLOTS = threading.BoundedSemaphore(2)
# Хеши с «перцем»: p1$ + argon2id(HMAC-SHA256(PASSWORD_PEPPER, пароль)). Перец хранится вне БД,
# поэтому по одному утёкшему файлу БД пароли нельзя перебирать даже медленно (аудит S-08)
PEPPER_PREFIX = "p1$"


def _peppered(password: str) -> str:
    return hmac.new(config.password_pepper.encode(), password.encode(), hashlib.sha256).hexdigest()


def hash_password(password: str) -> str:
    with ARGON2_SLOTS:
        if config.password_pepper:
            return PEPPER_PREFIX + _hasher.hash(_peppered(password))
        return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    target, candidate = password_hash or _DUMMY_HASH, password
    if target.startswith(PEPPER_PREFIX):
        if not config.password_pepper:
            return False  # перец потерян или не подключён: такой хеш проверить нельзя
        target, candidate = target[len(PEPPER_PREFIX):], _peppered(password)
    try:
        with ARGON2_SLOTS:
            return _hasher.verify(target, candidate) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """После успешного входа: перехешировать, если включили перец или поменялись параметры argon2."""
    peppered = password_hash.startswith(PEPPER_PREFIX)
    if bool(config.password_pepper) != peppered:
        return bool(config.password_pepper)  # снять перец без пароля нельзя — только добавить
    return _hasher.check_needs_rehash(password_hash.removeprefix(PEPPER_PREFIX))


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def tokens_equal(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a, b)


def _fernet() -> Fernet:
    if not config.secret_key:
        raise RuntimeError("SECRET_KEY не задан в .env — нельзя шифровать секреты")
    return Fernet(config.secret_key.encode())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode() if value else ""


def decrypt(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return ""
