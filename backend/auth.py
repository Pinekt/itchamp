"""
Аутентификация и разграничение доступа КТК ЭЛОУ-АВТ.

Роли и права (критерий «информационная безопасность»):

    действие                                  operator instructor admin
    вход, свои тренировки, команды в WS          +         +        +
    список всех тренировок, чужой разбор         −         +        +
    эталонные шаги сценария                      −         +        +
    управление пользователями и сценариями       −         −        +

Эталонные шаги закрыты от оператора намеренно: это «ответы» к сценарию,
обучаемый не должен видеть их до разбора с инструктором.

Каждая попытка входа и каждый отказ в доступе пишутся в audit_log.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, status

from . import security, storage
from .models import LoginRequest, UserInfo

router = APIRouter(tags=["auth"])


def client_ip(request: Request | WebSocket) -> str | None:
    """IP клиента с учётом обратного прокси (Nginx перед приложением)."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _user_agent(request: Request | WebSocket) -> str:
    return (request.headers.get("user-agent") or "")[:200]


async def _user_from_token(token: str | None) -> dict | None:
    """
    Восстановить пользователя по токену сеанса. Данные берём из БД, а не из
    токена: если учётную запись отключили или удалили, выданный ранее токен
    перестаёт действовать сразу, не дожидаясь истечения срока.
    """
    if not token:
        return None
    payload = security.decode_token(token)
    if not payload:
        return None
    try:
        user_id = int(payload.get("sub", ""))
    except (TypeError, ValueError):
        return None
    user = await storage.get_user_by_id(user_id)
    if user is None or not user["active"]:
        return None
    return user


# ------------------------------------------------------------- зависимости

async def optional_user(request: Request) -> dict | None:
    """Текущий пользователь или None — там, где вход не обязателен."""
    return await _user_from_token(request.cookies.get(security.COOKIE_NAME))


async def current_user(request: Request) -> dict:
    """Текущий пользователь. 401, если не выполнен вход."""
    user = await optional_user(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Требуется вход в систему")
    return user


def require_role(*roles: str):
    """
    Зависимость: допускать только перечисленные роли.
    Отказ фиксируется в журнале аудита — это требование по ИБ.
    """
    async def _check(request: Request, user: dict = Depends(current_user)) -> dict:
        if user["role"] not in roles:
            await storage.audit(user["id"], "access_denied",
                                {"path": request.url.path, "role": user["role"],
                                 "required": list(roles)}, ip=client_ip(request))
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="Недостаточно прав")
        return user
    return _check


async def ws_user(websocket: WebSocket) -> dict | None:
    """Пользователь для WebSocket-канала: cookie приходит вместе с рукопожатием."""
    return await _user_from_token(websocket.cookies.get(security.COOKIE_NAME))


# ------------------------------------------------------------------ маршруты

@router.post("/api/login", response_model=UserInfo)
async def login(body: LoginRequest, request: Request, response: Response) -> UserInfo:
    """
    Вход по логину и паролю. При успехе ставит cookie httpOnly с токеном сеанса.

    Защита от подбора: после MAX_FAILED_ATTEMPTS неудач подряд учётная запись
    временно блокируется. Ответ при неверном пароле и при несуществующем
    логине одинаков — чтобы нельзя было перебором узнать существующие логины.
    """
    ip = client_ip(request)
    login_name = body.login.strip()

    left = security.lockout_remaining(login_name)
    if left > 0:
        await storage.audit(None, "login_blocked",
                            {"login": login_name, "retry_after_s": left}, ip=ip)
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail=f"Слишком много неудачных попыток. "
                                   f"Повторите через {left} с",
                            headers={"Retry-After": str(left)})

    user = await storage.get_user_for_auth(login_name)
    ok = user is not None and user["active"] and \
        security.verify_password(user["password_hash"], body.password)

    if not ok:
        attempts = security.register_failure(login_name)
        await storage.audit(user["id"] if user else None, "login_failed",
                            {"login": login_name, "attempt": attempts,
                             "user_agent": _user_agent(request)}, ip=ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Неверный логин или пароль")

    security.register_success(login_name)

    # Перевод учётной записи со старого SHA-256 на Argon2id — прозрачно для
    # пользователя, пароль менять не требуется.
    if security.needs_rehash(user["password_hash"]):
        await storage.set_password_hash(user["id"], security.hash_password(body.password))
        await storage.audit(user["id"], "password_rehashed", {"algo": "argon2id"}, ip=ip)

    token = security.create_token(user["id"], user["login"], user["role"])
    response.set_cookie(
        key=security.COOKIE_NAME, value=token, httponly=True, samesite="strict",
        secure=security.COOKIE_SECURE, max_age=security.SESSION_TTL_HOURS * 3600, path="/",
    )
    await storage.audit(user["id"], "login_success",
                        {"login": user["login"], "role": user["role"],
                         "user_agent": _user_agent(request)}, ip=ip)
    return UserInfo(id=user["id"], login=user["login"],
                    full_name=user["full_name"], role=user["role"])


@router.post("/api/logout")
async def logout(request: Request, response: Response) -> dict:
    """Выход: удаляет cookie сеанса. Безопасен при повторном вызове."""
    user = await optional_user(request)
    response.delete_cookie(security.COOKIE_NAME, path="/")
    if user:
        await storage.audit(user["id"], "logout", {"login": user["login"]},
                            ip=client_ip(request))
    return {"detail": "Выход выполнен"}


@router.get("/api/me", response_model=UserInfo)
async def me(user: dict = Depends(current_user)) -> UserInfo:
    """Текущий пользователь — интерфейс по нему решает, что показывать."""
    return UserInfo(id=user["id"], login=user["login"],
                    full_name=user["full_name"], role=user["role"])
