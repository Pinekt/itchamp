"""
Администрирование КТК ЭЛОУ-АВТ: учётные записи и журнал аудита.

Роль `admin` до этого была объявлена, но ничего не умела сверх инструктора,
а журнал аудита писался и не читался — то есть требование «вести аудит»
выполнялось формально: посмотреть записи было нечем.

Что здесь закрывается по критерию информационной безопасности:
  • учётными записями управляет только администратор — заведение, роль,
    отключение, смена пароля;
  • каждое изменение попадает в журнал аудита вместе с тем, кто его сделал;
  • журнал доступен для просмотра с фильтрами по пользователю и событию.

Защита от потери управления: администратор не меняет роль и не отключает сам
себя. Без этого он запирал бы себя снаружи, а восстанавливать доступ пришлось
бы правкой БД руками.

Отдельной проверки «остался последний администратор» здесь нет намеренно —
она была бы недостижимой. Изменить роль или отключить учётную запись может
только действующий администратор; себя он тронуть не вправе, значит после
любого изменения сам вызывающий остаётся действующим администратором. Хотя бы
один администратор есть всегда, и это следует из проверки выше, а не из
отдельного подсчёта.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from . import auth, security, storage
from .models import (AuditEntry, PasswordReset, Role, UserCreate, UserDetail,
                     UserUpdate)

router = APIRouter(tags=["admin"])

#: Все маршруты этого модуля — только для администратора.
admin_only = auth.require_role(Role.ADMIN.value)


def _check_password(password: str) -> None:
    if len(password) < security.MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Пароль короче {security.MIN_PASSWORD_LENGTH} символов")


async def _get_user_or_404(user_id: int) -> dict:
    user = await storage.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Учётная запись не найдена")
    return user


# ------------------------------------------------------------ учётные записи

@router.get("/api/users", response_model=list[UserDetail])
async def list_users(admin: dict = Depends(admin_only)):
    """Все учётные записи. Пароли и их хеши наружу не отдаются."""
    return await storage.list_users()


@router.post("/api/users", response_model=UserDetail,
             status_code=status.HTTP_201_CREATED)
async def create_user(body: UserCreate, request: Request,
                      admin: dict = Depends(admin_only)):
    """Завести учётную запись. Логин занят — 409."""
    _check_password(body.password)
    created = await storage.create_user(
        login=body.login, full_name=body.full_name, role=body.role.value,
        password_hash=security.hash_password(body.password))
    if created is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Такой логин уже занят")
    await storage.audit(admin["id"], "user_created",
                        {"user_id": created["id"], "login": created["login"],
                         "role": created["role"]},
                        ip=auth.client_ip(request))
    return created


@router.patch("/api/users/{user_id}", response_model=UserDetail)
async def update_user(user_id: int, body: UserUpdate, request: Request,
                      admin: dict = Depends(admin_only)):
    """
    Изменить ФИО, роль или признак активности.

    Отключение и смена роли действуют немедленно: пользователь восстанавливается
    из БД на каждом запросе, а не берётся из токена, — выданный ранее токен
    сразу перестаёт давать прежние права.
    """
    target = await _get_user_or_404(user_id)
    changing_role = body.role is not None and body.role.value != target["role"]
    deactivating = body.active is False and target["active"]

    if target["id"] == admin["id"] and (changing_role or deactivating):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Нельзя изменить собственную роль или отключить себя")

    updated = await storage.update_user(
        user_id, full_name=body.full_name,
        role=body.role.value if body.role else None, active=body.active)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Учётная запись не найдена")

    changes = body.model_dump(exclude_none=True)
    if "role" in changes:
        changes["role"] = body.role.value
    await storage.audit(admin["id"], "user_updated",
                        {"user_id": user_id, "login": updated["login"],
                         "changes": changes},
                        ip=auth.client_ip(request))
    return updated


@router.post("/api/users/{user_id}/password")
async def reset_password(user_id: int, body: PasswordReset, request: Request,
                         admin: dict = Depends(admin_only)):
    """
    Назначить новый пароль. Сам пароль в журнал аудита не пишется — фиксируется
    только факт смены, кем и кому.
    """
    _check_password(body.password)
    target = await _get_user_or_404(user_id)
    await storage.set_password_hash(user_id, security.hash_password(body.password))
    # снимаем блокировку подбора: иначе назначенный пароль не подойдёт,
    # пока не истечёт наказание за прежние неудачные попытки
    security.register_success(target["login"])
    await storage.audit(admin["id"], "password_reset",
                        {"user_id": user_id, "login": target["login"]},
                        ip=auth.client_ip(request))
    return {"detail": "Пароль изменён"}


# -------------------------------------------------------------- журнал аудита

@router.get("/api/audit", response_model=list[AuditEntry])
async def read_audit(admin: dict = Depends(admin_only),
                     limit: int = Query(100, ge=1, le=1000),
                     offset: int = Query(0, ge=0),
                     user_id: int | None = None,
                     event: str | None = None):
    """Журнал аудита, свежие записи первыми, с фильтрами по пользователю и событию."""
    return await storage.list_audit(limit=limit, offset=offset,
                                    user_id=user_id, event=event)


@router.get("/api/audit/events", response_model=list[str])
async def audit_events(admin: dict = Depends(admin_only)):
    """Виды событий, встречающиеся в журнале, — для фильтра в интерфейсе."""
    return await storage.list_audit_events()
