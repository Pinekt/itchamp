#!/usr/bin/env python3
"""
Подготовка стенда к демонстрации.

Пустые экраны портят показ: список тренировок без строк, разбор без графика,
журнал аудита из трёх записей. Скрипт наполняет стенд историей, по которой
видно, что система работала не пять минут:

  • обучаемые с настоящими ФИО (Смирнов, Волков) и инструктор Петрова;
  • три проведённые тренировки с разным исходом — зачёт, провал с аварией
    и прерванная связью;
  • разбор одной из них уже открыт инструктором, другой — ещё нет: на показе
    видно обе стороны ограничения;
  • журнал аудита с входами, отказом в доступе и действиями администратора.

Запуск на локальном стенде:

    python scripts/demo_seed.py --url http://localhost:8000 --admin-password admin

На боевом стенде пароль администратора лежит в deploy/.env:

    sudo grep KTK_PASSWORD_ADMIN /opt/ktk/deploy/.env
    python scripts/demo_seed.py --url https://itchamp.root72.ru --admin-password '<пароль>'

Скрипт ничего не удаляет. Повторный запуск добавит ещё тренировок, а учётные
записи переиспользует, назначив им прежний пароль заново.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx
import websockets

#: Пароль демонстрационных обучаемых. Показывать его на видео не страшно —
#: стенд демонстрационный, а администраторский пароль сюда не попадает.
DEMO_PASSWORD = "Тренажёр-2026-демо"

TRAINEES = [
    dict(login="smirnov", full_name="Смирнов А. В.", role="operator"),
    dict(login="volkov", full_name="Волков Д. С.", role="operator"),
    dict(login="petrova", full_name="Петрова Е. И.", role="instructor"),
]


def say(text: str) -> None:
    print(f"\033[1;36m==>\033[0m {text}")


class Stand:
    """Клиент стенда: REST через httpx, канал тренировки через websockets."""

    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")
        self.ws_url = self.url.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
        self.client = httpx.AsyncClient(base_url=self.url, timeout=30.0, follow_redirects=False)

    async def close(self) -> None:
        await self.client.aclose()

    async def login(self, login: str, password: str) -> str:
        r = await self.client.post("/api/login", json={"login": login, "password": password})
        if r.status_code != 200:
            raise SystemExit(f"Вход {login} не удался: {r.status_code} {r.text[:200]}")
        return r.cookies.get("ktk_session") or self.client.cookies.get("ktk_session")

    async def ensure_user(self, login: str, full_name: str, role: str) -> int:
        """Завести учётную запись или переиспользовать, назначив известный пароль."""
        r = await self.client.post("/api/users", json={
            "login": login, "full_name": full_name, "role": role,
            "password": DEMO_PASSWORD})
        if r.status_code == 201:
            return r.json()["id"]
        if r.status_code != 409:
            raise SystemExit(f"Не удалось завести {login}: {r.status_code} {r.text[:200]}")
        users = (await self.client.get("/api/users")).json()
        uid = next(u["id"] for u in users if u["login"] == login)
        await self.client.post(f"/api/users/{uid}/password", json={"password": DEMO_PASSWORD})
        return uid


async def run_training(stand: Stand, cookie: str, scenario: str,
                       script: list[tuple[int, dict]], ticks: int,
                       finish: bool = True) -> str | None:
    """
    Провести тренировку по сценарию.

    `script` — что и на каком такте нажимает обучаемый. `finish=False`
    обрывает связь без завершения: так получается прерванная тренировка,
    по которой оценка всё равно формируется.
    """
    plan = dict(script)
    session_id = None
    async with websockets.connect(
            stand.ws_url, additional_headers={"Cookie": f"ktk_session={cookie}"},
            open_timeout=30, close_timeout=5) as ws:
        await ws.send(json.dumps({"session_action": "start", "scenario": scenario}))
        seen = 0
        while seen < ticks:
            msg = json.loads(await ws.recv())
            if msg.get("type") != "state":
                continue
            seen += 1
            if seen in plan:
                await ws.send(json.dumps(plan[seen]))
        if not finish:
            return None                      # выходим из with — связь рвётся
        await ws.send(json.dumps({"session_action": "stop"}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("type") == "assessment":
                payload = msg["payload"]
                session_id = payload["session_id"]
                print(f"    балл {payload['total_score']:.0f}, "
                      f"вердикт {payload['verdict']}, "
                      f"ошибок {payload['errors_count']}")
                break
    return session_id


async def main(args) -> None:
    admin = Stand(args.url)
    say(f"Вход администратором на {args.url}")
    await admin.login("admin", args.admin_password)

    say("Завожу учётные записи демонстрации")
    for t in TRAINEES:
        uid = await admin.ensure_user(**t)
        print(f"    {t['login']:9} {t['full_name']:16} {t['role']:11} id={uid}")

    # --- тренировка 1: образцовый пуск, зачёт -----------------------------
    say("Тренировка 1 — Смирнов, штатный пуск (ожидается зачёт)")
    smirnov = Stand(args.url)
    cookie = await smirnov.login("smirnov", DEMO_PASSWORD)
    good = await run_training(smirnov, cookie, "startup", [
        (2, {"action": "set_pump", "target": "PUMP_1", "value": 1}),
        (3, {"action": "set_valve", "target": "VALVE_FEED", "value": 70}),
        (4, {"action": "start", "target": None, "value": None}),
    ], ticks=args.ticks)

    # --- тренировка 2: авария, оператор ошибается -------------------------
    say("Тренировка 2 — Волков, рост давления (ожидается провал)")
    volkov = Stand(args.url)
    cookie_v = await volkov.login("volkov", DEMO_PASSWORD)
    bad = await run_training(volkov, cookie_v, "pressure_alarm", [
        # открывает клапан вместо того, чтобы прикрыть — давление уходит вверх
        (3, {"action": "set_valve", "target": "VALVE_FEED", "value": 100}),
        # спохватывается поздно, норматив шага уже вышел
        (int(args.ticks * 0.8), {"action": "set_valve", "target": "VALVE_FEED", "value": 35}),
    ], ticks=args.ticks)

    # --- тренировка 3: обрыв связи ----------------------------------------
    say("Тренировка 3 — Смирнов, обрыв связи без завершения")
    await run_training(smirnov, cookie, "pump_trip", [
        (2, {"action": "ack_alarm", "target": None, "value": None}),
    ], ticks=max(6, args.ticks // 3), finish=False)
    print("    связь оборвана — оценка формируется на стороне сервера")

    # --- инструктор открывает разбор только одной тренировки ---------------
    say("Петрова открывает разбор Смирнову (разбор Волкова остаётся закрытым)")
    petrova = Stand(args.url)
    await petrova.login("petrova", DEMO_PASSWORD)
    if good:
        r = await petrova.client.post(f"/api/sessions/{good}/release")
        print(f"    разбор {good[:8]}: {r.status_code}")

    # --- след в журнале аудита: отказ в доступе ---------------------------
    say("Оставляю в журнале аудита отказ в доступе и неудачный вход")
    if bad:
        # оператор пытается открыть чужую тренировку — 403 и запись в аудите
        await smirnov.client.get(f"/api/sessions/{bad}/debrief")
    await httpx.AsyncClient(base_url=args.url, timeout=15.0).post(
        "/api/login", json={"login": "smirnov", "password": "не тот пароль"})

    for s in (admin, smirnov, volkov, petrova):
        await s.close()

    print()
    print("\033[1;32m=== Стенд готов к показу ===\033[0m")
    print(f"Обучаемые и инструктор входят паролем: {DEMO_PASSWORD}")
    print("  smirnov  — зачёт, разбор открыт инструктором")
    print("  volkov   — не сдано, разбор эталонных шагов ещё закрыт")
    print("  petrova  — инструктор, видит обе тренировки")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Наполнить стенд данными для демонстрации")
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--admin-password", required=True,
                   help="пароль admin; на стенде — в deploy/.env")
    p.add_argument("--ticks", type=int, default=25,
                   help="длительность тренировки в тактах (на стенде такт — 1 с)")
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        sys.exit(1)
