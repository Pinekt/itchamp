"""
Автотесты аутентификации и разграничения доступа (критерий ИБ).

Проверяем то, что будут проверять на защите: без входа система закрыта,
роли реально ограничивают доступ, подбор пароля блокируется, а всё
существенное попадает в журнал аудита.
"""
from __future__ import annotations

import pytest

from backend import security
from conftest import as_json, login_as


# ------------------------------------------------------------------- вход

def test_login_success_sets_httponly_cookie(client):
    """Верные учётные данные → сеанс и cookie, недоступная скриптам."""
    r = login_as(client, "operator")
    assert r.status_code == 200
    body = r.json()
    assert body["login"] == "operator" and body["role"] == "operator"
    assert body["full_name"] == "Оператор-стажёр"

    raw = r.headers["set-cookie"]
    assert security.COOKIE_NAME in raw
    assert "HttpOnly" in raw           # недоступна из JavaScript (защита от XSS)
    assert "SameSite=strict" in raw    # защита от CSRF


def test_login_wrong_password_rejected(client):
    r = login_as(client, "operator", "неверный")
    assert r.status_code == 401
    assert security.COOKIE_NAME not in client.cookies


def test_unknown_login_looks_the_same_as_wrong_password(client):
    """Ответы совпадают — перебором нельзя выяснить существующие логины."""
    unknown = client.post("/api/login", json={"login": "нет-такого", "password": "x"})
    wrong = login_as(client, "operator", "неверный")
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


def test_passwords_stored_as_argon2_not_plaintext(client, db_query):
    """В БД лежит хеш Argon2id, а не открытый пароль и не SHA-256."""
    stored = db_query("SELECT password_hash FROM users WHERE login='operator'")[0][0]
    assert stored.startswith("$argon2id$")
    assert stored != "operator"
    assert not security.is_legacy_hash(stored)
    assert security.verify_password(stored, "operator")
    assert not security.verify_password(stored, "operator ")


def test_legacy_sha256_hash_still_accepted_and_upgraded(client):
    """Старые базы продолжают работать: SHA-256 распознаётся и требует перехеширования."""
    import hashlib
    legacy = hashlib.sha256(b"operator").hexdigest()
    assert security.is_legacy_hash(legacy)
    assert security.verify_password(legacy, "operator")
    assert not security.verify_password(legacy, "другой")
    assert security.needs_rehash(legacy)


# --------------------------------------------------------- защита от подбора

def test_bruteforce_lockout(client):
    """После KTK_MAX_FAILED_ATTEMPTS неудач подряд учётная запись блокируется."""
    for _ in range(security.MAX_FAILED_ATTEMPTS):
        assert login_as(client, "admin", "мимо").status_code == 401

    blocked = login_as(client, "admin", "мимо")
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers

    # даже верный пароль отклоняется, пока действует блокировка
    assert login_as(client, "admin").status_code == 429


def test_successful_login_resets_failure_counter(client):
    login_as(client, "operator", "мимо")
    assert login_as(client, "operator").status_code == 200
    assert security.lockout_remaining("operator") == 0


# ------------------------------------------------------- доступ без входа

@pytest.mark.parametrize("path", [
    "/api/me", "/api/scenarios", "/api/sessions",
    "/api/scenarios/pump_trip/reference",
])
def test_endpoints_closed_without_login(client, path):
    assert client.get(path).status_code == 401


def test_root_redirects_to_login_page(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_login_page_is_public(client):
    assert client.get("/login").status_code == 200


def test_invalid_token_is_rejected(client):
    """Подделанный токен не даёт доступа."""
    client.cookies.set(security.COOKIE_NAME, "not.a.token")
    assert client.get("/api/me").status_code == 401


def test_token_signed_with_other_key_is_rejected(client):
    """Токен с чужой подписью не принимается — проверяется именно подпись."""
    import jwt
    from datetime import datetime, timedelta, timezone
    forged = jwt.encode({"sub": "1", "login": "admin", "role": "admin",
                         "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
                        "чужой-ключ", algorithm="HS256")
    client.cookies.set(security.COOKIE_NAME, forged)
    assert client.get("/api/me").status_code == 401


# ------------------------------------------------------ разграничение ролей

def test_operator_cannot_read_reference_steps(client):
    """Эталонные шаги — «ответы» к сценарию, оператору закрыты."""
    login_as(client, "operator")
    assert client.get("/api/scenarios/pump_trip/reference").status_code == 403


def test_instructor_can_read_reference_steps(client):
    login_as(client, "instructor")
    r = client.get("/api/scenarios/pump_trip/reference")
    assert r.status_code == 200 and len(r.json()) == 3


def test_operator_sees_only_own_sessions(client):
    """Оператор в списке тренировок видит только свои."""
    login_as(client, "operator")
    operator_id = client.get("/api/me").json()["id"]
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"session_action": "start", "scenario": "startup"})
        ws.receive_json()                      # дождаться первого тика
    own = client.get("/api/sessions").json()
    assert own and all(s["user_id"] == operator_id for s in own)


def test_operator_cannot_open_foreign_session(client):
    """Чужую тренировку оператор не откроет — 403 и запись в аудите."""
    # тренировку заводит инструктор
    login_as(client, "instructor")
    instructor_id = client.get("/api/me").json()["id"]
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"session_action": "start", "scenario": "startup"})
        ws.receive_json()
    # именно свою, а не первую в списке: инструктор видит все тренировки,
    # и полагаться на порядок выдачи здесь незачем
    foreign = next(s["id"] for s in client.get("/api/sessions").json()
                   if s["user_id"] == instructor_id)

    client.cookies.clear()
    login_as(client, "operator")
    for path in ("journal", "errors", "assessment"):
        assert client.get(f"/api/sessions/{foreign}/{path}").status_code == 403


def test_instructor_can_open_foreign_session(client):
    login_as(client, "operator")
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"session_action": "start", "scenario": "startup"})
        ws.receive_json()
    sid = client.get("/api/sessions").json()[0]["id"]

    client.cookies.clear()
    login_as(client, "instructor")
    assert client.get(f"/api/sessions/{sid}/journal").status_code == 200


def test_unknown_session_gives_404(client):
    login_as(client, "instructor")
    assert client.get("/api/sessions/нет-такой/journal").status_code == 404


# ---------------------------------------------------------------- WebSocket

def test_websocket_requires_login(client):
    """Канал тренировки без сеанса закрывается кодом 4401."""
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4401


def test_websocket_binds_session_to_logged_in_user(client):
    """Тренировка привязывается к вошедшему пользователю, а не к 'unknown'."""
    login_as(client, "operator")
    me = client.get("/api/me").json()
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"session_action": "start", "scenario": "pump_trip"})
        ws.receive_json()
    row = client.get("/api/sessions").json()[0]
    assert row["user_id"] == me["id"]
    assert row["operator"] == me["full_name"]


# -------------------------------------------------------------------- выход

def test_logout_clears_session(client):
    login_as(client, "operator")
    assert client.get("/api/me").status_code == 200
    assert client.post("/api/logout").status_code == 200
    client.cookies.clear()                       # браузер удаляет cookie по Set-Cookie
    assert client.get("/api/me").status_code == 401


def test_logout_without_login_is_safe(client):
    assert client.post("/api/logout").status_code == 200


# ---------------------------------------------------------------- аудит (ИБ)

def test_audit_records_successful_login(client, db_query):
    login_as(client, "instructor")
    rows = db_query("SELECT a.event, u.login FROM audit_log a JOIN users u ON u.id=a.user_id "
                    "WHERE a.event='login_success' ORDER BY a.id DESC LIMIT 1")
    assert rows and rows[0] == ("login_success", "instructor")


def test_audit_records_failed_login_with_attempt_number(client, db_query):
    login_as(client, "admin", "мимо")
    event, details = db_query("SELECT event, details FROM audit_log "
                              "WHERE event='login_failed' ORDER BY id DESC LIMIT 1")[0]
    assert event == "login_failed"
    details = as_json(details)
    assert details["login"] == "admin" and details["attempt"] == 1


def test_audit_records_lockout(client, db_query):
    for _ in range(security.MAX_FAILED_ATTEMPTS + 1):
        login_as(client, "operator", "мимо")
    events = [r[0] for r in db_query("SELECT event FROM audit_log ORDER BY id DESC LIMIT 5")]
    assert "login_blocked" in events


def test_audit_records_access_denied(client, db_query):
    login_as(client, "operator")
    client.get("/api/scenarios/pump_trip/reference")     # 403
    event, details = db_query("SELECT event, details FROM audit_log "
                              "WHERE event='access_denied' ORDER BY id DESC LIMIT 1")[0]
    assert event == "access_denied"
    details = as_json(details)
    assert details["path"].endswith("/reference")
    assert details["role"] == "operator"


def test_audit_records_logout(client, db_query):
    login_as(client, "operator")
    client.post("/api/logout")
    events = [r[0] for r in db_query("SELECT event FROM audit_log ORDER BY id DESC LIMIT 3")]
    assert "logout" in events


def test_audit_binds_training_session_to_user(client, db_query):
    """Старт тренировки в аудите привязан к пользователю, а не к NULL."""
    login_as(client, "operator")
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"session_action": "start", "scenario": "startup"})
        ws.receive_json()
    user_id = db_query("SELECT user_id FROM audit_log WHERE event='session_start' "
                       "ORDER BY id DESC LIMIT 1")[0][0]
    assert user_id == client.get("/api/me").json()["id"]
