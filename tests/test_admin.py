"""
Автотесты администрирования: учётные записи и журнал аудита.

Проверяется то, ради чего раздел и делался:
  • управлять учётными записями может только администратор;
  • отключение и смена роли действуют немедленно, не дожидаясь истечения токена;
  • администратор не может отключить или разжаловать сам себя — иначе
    восстанавливать доступ пришлось бы правкой БД руками, и из этого же следует,
    что действующий администратор в системе остаётся всегда;
  • каждое изменение попадает в журнал аудита, а сам журнал можно прочитать
    с фильтрами;
  • пароль не утекает наружу: ни в списке учётных записей, ни в аудите.
"""
from __future__ import annotations

import pytest

from conftest import login_as

GOOD_PASSWORD = "Пароль-стенда-2026"


@pytest.fixture()
def as_admin(client):
    """Сеанс администратора."""
    login_as(client, "admin")
    return client.get("/api/me").json()


def make_user(client, login: str, role: str = "operator",
              password: str = GOOD_PASSWORD, full_name: str | None = None):
    return client.post("/api/users", json={
        "login": login, "full_name": full_name or f"Пользователь {login}",
        "role": role, "password": password})


# --------------------------------------------------------- доступ к разделу

@pytest.mark.parametrize("path", ["/api/users", "/api/audit", "/api/audit/events"])
def test_admin_endpoints_closed_without_login(client, path):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("role", ["operator", "instructor"])
def test_only_admin_manages_users(client, role):
    """Оператору и инструктору раздел закрыт, отказ пишется в аудит."""
    login_as(client, role)
    assert client.get("/api/users").status_code == 403
    assert client.get("/api/audit").status_code == 403
    assert make_user(client, "stranger").status_code == 403

    client.cookies.clear()
    login_as(client, "admin")
    denied = client.get("/api/audit?event=access_denied").json()
    assert any(r["details"].get("path") == "/api/users" for r in denied)


def test_admin_page_requires_login(client):
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


# ------------------------------------------------------ заведение учётных записей

def test_create_user_then_login(client, as_admin):
    """Заведённая учётная запись сразу пригодна для входа."""
    r = make_user(client, "newbie", role="operator")
    assert r.status_code == 201
    created = r.json()
    assert created["login"] == "newbie" and created["active"] is True

    client.cookies.clear()
    assert login_as(client, "newbie", GOOD_PASSWORD).status_code == 200
    assert client.get("/api/me").json()["role"] == "operator"


def test_password_and_hash_never_returned(client, as_admin):
    """Ни пароль, ни его хеш наружу не отдаются."""
    make_user(client, "secret")
    body = client.get("/api/users").text
    assert GOOD_PASSWORD not in body and "argon2" not in body.lower()
    assert all("password" not in u and "password_hash" not in u
               for u in client.get("/api/users").json())


def test_duplicate_login_rejected(client, as_admin):
    make_user(client, "dup")
    assert make_user(client, "dup").status_code == 409


def test_short_password_rejected(client, as_admin):
    assert make_user(client, "weakpw", password="123").status_code == 422


@pytest.mark.parametrize("login", ["с пробелом", "кириллица", "!!!", "ab"])
def test_invalid_login_rejected(client, as_admin, login):
    """Логин ограничен латиницей: он попадает в журнал аудита и на стенд."""
    assert make_user(client, login).status_code == 422


# ------------------------------------------------------ изменение и отключение

def test_role_change_takes_effect_immediately(client, as_admin):
    """
    Пользователь восстанавливается из БД, а не из токена, поэтому новая роль
    действует на уже выданном сеансе — перевходить не нужно.
    """
    uid = make_user(client, "promoted").json()["id"]
    client.cookies.clear()
    login_as(client, "promoted", GOOD_PASSWORD)
    assert client.get("/api/scenarios/startup/reference").status_code == 403
    operator_cookies = dict(client.cookies)

    client.cookies.clear()
    login_as(client, "admin")
    assert client.patch(f"/api/users/{uid}", json={"role": "instructor"}).status_code == 200

    client.cookies.clear()
    client.cookies.update(operator_cookies)          # тот же токен, что и до повышения
    assert client.get("/api/scenarios/startup/reference").status_code == 200


def test_deactivated_user_loses_access_at_once(client, as_admin):
    """Отключение прекращает доступ немедленно, не дожидаясь истечения токена."""
    uid = make_user(client, "fired").json()["id"]
    client.cookies.clear()
    login_as(client, "fired", GOOD_PASSWORD)
    assert client.get("/api/me").status_code == 200
    stale = dict(client.cookies)

    client.cookies.clear()
    login_as(client, "admin")
    assert client.patch(f"/api/users/{uid}", json={"active": False}).status_code == 200

    client.cookies.clear()
    client.cookies.update(stale)
    assert client.get("/api/me").status_code == 401   # выданный ранее токен не работает

    client.cookies.clear()
    assert login_as(client, "fired", GOOD_PASSWORD).status_code == 401


def test_full_name_change_does_not_touch_role(client, as_admin):
    uid = make_user(client, "renamed", role="instructor").json()["id"]
    r = client.patch(f"/api/users/{uid}", json={"full_name": "Новое ФИО"})
    assert r.status_code == 200
    assert r.json()["full_name"] == "Новое ФИО" and r.json()["role"] == "instructor"


def test_update_unknown_user_gives_404(client, as_admin):
    assert client.patch("/api/users/999999", json={"active": False}).status_code == 404


# -------------------------------------------------- защита от потери управления

def test_admin_cannot_disable_or_demote_self(client, as_admin):
    """Иначе администратор запирает сам себя снаружи."""
    me = as_admin["id"]
    assert client.patch(f"/api/users/{me}", json={"active": False}).status_code == 409
    assert client.patch(f"/api/users/{me}", json={"role": "operator"}).status_code == 409
    assert client.get("/api/me").json()["role"] == "admin"


def test_at_least_one_admin_always_remains(client, as_admin):
    """
    Инвариант «хотя бы один действующий администратор» держится проверкой выше,
    без отдельного подсчёта: снять права может только администратор, а себя он
    тронуть не вправе — значит сам остаётся администратором.

    Тест работает на собственных учётных записях и сеедового `admin` не трогает:
    база у тестов общая, и разжалованный админ сломал бы всё, что идёт следом.
    """
    first = make_user(client, "adm_first", role="admin").json()["id"]
    second = make_user(client, "adm_second", role="admin").json()["id"]

    client.cookies.clear()
    login_as(client, "adm_first", GOOD_PASSWORD)

    # чужие права снять можно — управление не парализовано
    assert client.patch(f"/api/users/{second}", json={"active": False}).status_code == 200
    # свои — нет, поэтому действующий администратор в системе остаётся
    assert client.patch(f"/api/users/{first}", json={"role": "operator"}).status_code == 409
    assert client.patch(f"/api/users/{first}", json={"active": False}).status_code == 409

    me = client.get("/api/me").json()
    assert me["role"] == "admin" and me["id"] == first


def test_admin_can_demote_another_admin(client, as_admin):
    """Когда администраторов несколько, разжаловать коллегу разрешено."""
    target = make_user(client, "adm_target", role="admin").json()["id"]
    make_user(client, "adm_actor", role="admin")

    client.cookies.clear()
    login_as(client, "adm_actor", GOOD_PASSWORD)
    r = client.patch(f"/api/users/{target}", json={"role": "instructor"})
    assert r.status_code == 200 and r.json()["role"] == "instructor"


# ------------------------------------------------------------- смена пароля

def test_password_reset_lets_user_in_with_new_password(client, as_admin):
    uid = make_user(client, "forgetful").json()["id"]
    new = "Новый-пароль-стенда"
    assert client.post(f"/api/users/{uid}/password", json={"password": new}).status_code == 200

    client.cookies.clear()
    assert login_as(client, "forgetful", GOOD_PASSWORD).status_code == 401
    assert login_as(client, "forgetful", new).status_code == 200


def test_password_reset_clears_bruteforce_lockout(client, as_admin):
    """
    Смена пароля снимает блокировку подбора: иначе назначенный пароль не
    подошёл бы, пока не истечёт наказание за прежние неудачные попытки.
    """
    uid = make_user(client, "locked").json()["id"]
    admin_cookies = dict(client.cookies)

    client.cookies.clear()
    for _ in range(3):                                # KTK_MAX_FAILED_ATTEMPTS=3
        login_as(client, "locked", "мимо")
    assert login_as(client, "locked", GOOD_PASSWORD).status_code == 429

    client.cookies.update(admin_cookies)
    new = "Пароль-после-разбора"
    assert client.post(f"/api/users/{uid}/password", json={"password": new}).status_code == 200

    client.cookies.clear()
    assert login_as(client, "locked", new).status_code == 200


def test_short_password_on_reset_rejected(client, as_admin):
    uid = make_user(client, "shortpw").json()["id"]
    assert client.post(f"/api/users/{uid}/password", json={"password": "12"}).status_code == 422


# ------------------------------------------------------------- журнал аудита

def test_admin_actions_are_audited(client, as_admin):
    """Заведение, изменение и смена пароля видны в журнале с автором."""
    uid = make_user(client, "audited").json()["id"]
    client.patch(f"/api/users/{uid}", json={"role": "instructor"})
    client.post(f"/api/users/{uid}/password", json={"password": "Ещё-один-пароль"})

    # записи идут свежими вперёд, поэтому берём первое вхождение каждого события:
    # база у тестов общая, и дальше по списку лежат следы предыдущих тестов
    events: dict[str, dict] = {}
    for r in client.get("/api/audit?limit=50").json():
        events.setdefault(r["event"], r)
    assert {"user_created", "user_updated", "password_reset"} <= set(events)
    assert events["user_created"]["details"]["login"] == "audited"
    assert events["user_updated"]["details"]["changes"]["role"] == "instructor"
    # автором изменений записан администратор, а не изменяемый пользователь
    assert all(events[e]["user_id"] == as_admin["id"]
               for e in ("user_created", "user_updated", "password_reset"))


def test_audit_never_contains_password(client, as_admin):
    """В журнал пишется факт смены пароля, но не сам пароль."""
    uid = make_user(client, "quiet").json()["id"]
    client.post(f"/api/users/{uid}/password", json={"password": GOOD_PASSWORD})
    assert GOOD_PASSWORD not in client.get("/api/audit?limit=200").text


def test_audit_shows_login_next_to_user_id(client, as_admin):
    """Читать журнал по числовым идентификаторам неудобно — подставляем логин."""
    rows = client.get("/api/audit?event=login_success&limit=10").json()
    assert rows and rows[0]["login"] == "admin"


def test_audit_filters_by_user_and_event(client, as_admin):
    make_user(client, "filtered")
    by_event = client.get("/api/audit?event=user_created&limit=50").json()
    assert by_event and all(r["event"] == "user_created" for r in by_event)

    by_user = client.get(f"/api/audit?user_id={as_admin['id']}&limit=50").json()
    assert by_user and all(r["user_id"] == as_admin["id"] for r in by_user)


def test_audit_is_newest_first_and_paginated(client, as_admin):
    rows = client.get("/api/audit?limit=5").json()
    assert len(rows) <= 5
    assert [r["id"] for r in rows] == sorted((r["id"] for r in rows), reverse=True)

    second = client.get("/api/audit?limit=5&offset=5").json()
    assert not ({r["id"] for r in rows} & {r["id"] for r in second})


def test_audit_events_list_is_offered_for_filter(client, as_admin):
    events = client.get("/api/audit/events").json()
    assert "login_success" in events and "user_created" in events
