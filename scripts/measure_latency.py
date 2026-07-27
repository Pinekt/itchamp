#!/usr/bin/env python3
"""
Замер времени отклика и задержек КТК ЭЛОУ-АВТ.

Требование критерия технической реализации — подтверждать заявленное время
отклика цифрами, а не словами. Скрипт снимает четыре величины, каждая из
которых заметна оператору:

  1. вход в систему           — сколько занимает Argon2id (намеренно медленный);
  2. отклик REST              — открытие списков и разбора тренировки;
  3. отклик на команду        — от нажатия на мнемосхеме до подтверждения в журнале;
  4. период выдачи телеметрии — насколько ровно идут такты симуляции.

Запуск (сервер поднимается сам, база — SQLite во временном файле):

    python scripts/measure_latency.py
    python scripts/measure_latency.py --out docs/performance.md

Замер на уже поднятом стенде (например, в docker compose с PostgreSQL):

    python scripts/measure_latency.py --url http://localhost:8000 \
                                      --login operator --password operator

Замеры снимаются с той же машины, где работает сервер: сетевая задержка в
них не входит. Это осознанно — так виден вклад именно приложения и БД.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


#: Нормативы на p95, мс. Приняты командой и обоснованы так:
#:   command     — 200 мс: порог, ниже которого отклик воспринимается человеком
#:                  как мгновенный; на реальном щите КИПиА оператор ждёт того же;
#:   rest        — 300 мс: открытие списка или разбора без ощущения задержки;
#:   login       — 500 мс: вход намеренно медленный (Argon2id), но не раздражающий;
#:   tick_slack  — 50 мс: допустимое отклонение периода телеметрии от такта,
#:                  5 % — на глаз незаметно и не искажает время реакции в журнале;
#:   engine_step — 50 мс: шаг матмодели должен идти с большим запасом к такту 1 с,
#:                  иначе усложнять цифровой двойник будет некуда.
TARGETS = dict(command=200.0, rest=300.0, login=500.0, tick_slack=50.0,
               engine_step=50.0)


# --------------------------------------------------------------- статистика

def percentile(values: list[float], p: float) -> float:
    """Перцентиль по линейной интерполяции (как в numpy, но без numpy)."""
    if not values:
        return float("nan")
    xs = sorted(values)
    k = (len(xs) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return xs[int(k)]
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


class Metric:
    """
    Набор замеров одной величины, в миллисекундах.

    `target` — принятый командой норматив на p95. Сравнение с ним и делает из
    списка чисел ответ на вопрос критерия «укладывается ли система в отклик».
    """

    def __init__(self, name: str, note: str = "", target: float | None = None) -> None:
        self.name, self.note, self.target = name, note, target
        self.samples: list[float] = []

    def add(self, seconds: float) -> None:
        self.samples.append(seconds * 1000.0)

    @property
    def p95(self) -> float:
        return percentile(self.samples, 95)

    @property
    def ok(self) -> bool | None:
        return None if self.target is None else self.p95 <= self.target

    def row(self) -> str:
        s = self.samples
        # доли миллисекунды на одном знаке после запятой превращаются в «0.0»,
        # поэтому для быстрых величин показываем больше знаков
        d = 1 if max(s) >= 1.0 else 3
        cells = [statistics.mean(s), percentile(s, 50), self.p95,
                 percentile(s, 99), max(s)]
        verdict = "—" if self.target is None else \
            f"≤ {self.target:.0f} · {'уложились' if self.ok else 'ПРЕВЫШЕН'}"
        return (f"| {self.name} | {len(s)} | "
                + " | ".join(f"{v:.{d}f}" for v in cells)
                + f" | {verdict} | {self.note} |")


TABLE_HEADER = ("| Показатель | Замеров | Среднее | p50 | p95 | p99 | Макс | Норматив p95 | Примечание |\n"
                "|---|---:|---:|---:|---:|---:|---:|---|---|")


# ------------------------------------------------------------- запуск сервера

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(tick_period: float) -> tuple[subprocess.Popen, str, Path]:
    """Поднять сервер на свободном порту с отдельной базой SQLite."""
    port = free_port()
    db = Path(tempfile.gettempdir()) / f"ktk_bench_{port}.db"
    db.unlink(missing_ok=True)
    env = dict(os.environ,
               DATABASE_URL=f"sqlite+aiosqlite:///{db}",
               KTK_SECRET_KEY="ключ-замеров-длиной-не-меньше-32-байт-для-HS256",
               KTK_TICK_PERIOD_S=str(tick_period))
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env)
    return proc, f"http://127.0.0.1:{port}", db


async def wait_ready(url: str, timeout: float = 40.0) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient() as c:
        while time.monotonic() < deadline:
            try:
                if (await c.get(f"{url}/login", timeout=2.0)).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
    raise RuntimeError("сервер не поднялся за отведённое время")


# ----------------------------------------------------------------- сами замеры

async def measure_login(url: str, login: str, password: str, n: int) -> tuple[Metric, str]:
    """
    Вход в систему. Argon2id считается десятки миллисекунд намеренно: это цена
    стойкости пароля к перебору, и в норматив отклика на команду она не входит —
    вход выполняется один раз за смену.
    """
    m = Metric("Вход в систему (Argon2id)", "один раз за смену", target=TARGETS["login"])
    cookie = ""
    async with httpx.AsyncClient(base_url=url) as c:
        for _ in range(n):
            t0 = time.perf_counter()
            r = await c.post("/api/login", json={"login": login, "password": password})
            m.add(time.perf_counter() - t0)
            r.raise_for_status()
            cookie = r.cookies.get("ktk_session") or cookie
    return m, cookie


async def measure_rest(client: httpx.AsyncClient, path: str, name: str,
                       n: int, note: str = "", target: float | None = None) -> Metric:
    m = Metric(name, note, target)
    for _ in range(10):                       # прогрев: первый запрос ловит открытие пула БД
        (await client.get(path)).raise_for_status()
    for _ in range(n):
        t0 = time.perf_counter()
        r = await client.get(path)
        m.add(time.perf_counter() - t0)
        r.raise_for_status()
    return m


async def measure_ws(url: str, cookie: str, scenario: str, commands: int,
                     ticks: int, tick_period: float) -> tuple[Metric, Metric, str]:
    """
    Провести тренировку и снять две величины:
      • отклик на команду оператора — от отправки до подтверждения в журнале;
      • период выдачи телеметрии — интервалы между соседними снимками состояния.

    Обе снимаются на идущей тренировке, то есть команды соперничают за канал
    с потоком телеметрии — так же, как у оператора на рабочем месте.
    """
    ws_url = url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
    cmd_m = Metric("Отклик на команду оператора", "на идущей тренировке",
                   target=TARGETS["command"])
    tick_m = Metric("Период выдачи телеметрии", "интервал между снимками состояния",
                    target=tick_period * 1000 + TARGETS["tick_slack"])
    session_id = ""

    async with websockets.connect(ws_url, additional_headers={"Cookie": f"ktk_session={cookie}"}) as ws:
        await ws.send(json.dumps({"session_action": "start", "scenario": scenario}))

        async def read_until(kind: str) -> dict:
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("type") == "state":
                    now = time.perf_counter()
                    if state_times:
                        tick_m.add(now - state_times[-1])
                    state_times.append(now)
                if msg.get("type") == kind:
                    return msg

        state_times: list[float] = []
        await read_until("state")

        # отклик на команду: клапан двигают чаще всего, его и меряем
        for i in range(commands):
            t0 = time.perf_counter()
            await ws.send(json.dumps({"action": "set_valve", "target": "VALVE_FEED",
                                      "value": 40 + i % 50}))
            await read_until("action")
            cmd_m.add(time.perf_counter() - t0)

        # ровность тактов: копим интервалы, пока не наберём нужное число
        while len(tick_m.samples) < ticks:
            await read_until("state")

        await ws.send(json.dumps({"session_action": "stop"}))
        session_id = (await read_until("assessment"))["payload"]["session_id"]

    return cmd_m, tick_m, session_id


def inflate_telemetry(db: Path, session_id: str, points: int) -> None:
    """
    Дописать в тренировку синтетическую телеметрию, чтобы измерить разбор на
    реальном объёме: смена оператора — это не тридцать секунд, а десятки минут,
    то есть тысячи точек. Ждать их в реальном времени незачем — форма записи
    та же самая, а нас интересует, как отклик зависит от количества строк.

    Пишем напрямую в файл базы: сервер в этот момент простаивает.
    """
    import sqlite3

    conn = sqlite3.connect(db, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        start = conn.execute("SELECT COALESCE(MAX(t), 0) FROM telemetry "
                             "WHERE session_id = ?", (session_id,)).fetchone()[0]
        rows = [(session_id, start + i + 1, 120 + i % 60, 340 + i % 20,
                 90 + i % 30, 50 + i % 40, 1, "[]") for i in range(points)]
        conn.executemany(
            "INSERT INTO telemetry (session_id, t, pressure, temperature, flow, "
            "level, running, alarms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


def measure_engine_step(n: int) -> Metric:
    """
    Чистое время шага цифрового двойника, без сети и БД — запас по
    производительности матмодели: во сколько раз шаг быстрее такта.
    """
    from backend.engine import SimulationEngine

    m = Metric("Шаг матмодели (без сети и БД)", "запас цифрового двойника",
               target=TARGETS["engine_step"])
    eng = SimulationEngine()
    eng.running = True
    for _ in range(200):                      # прогрев
        eng.step(1.0)
    for _ in range(n):
        t0 = time.perf_counter()
        eng.step(1.0)
        m.add(time.perf_counter() - t0)
    return m


# ------------------------------------------------------------------- отчёт

def conclusions(metrics: list[Metric], tick_period: float) -> list[str]:
    """
    Вывод по числам. Пишется здесь, а не руками в документе: отчёт
    перевыпускается скриптом, и вывод должен обновляться вместе с замерами.
    """
    by = {m.name: m for m in metrics}
    out: list[str] = []

    cmd = by.get("Отклик на команду оператора")
    if cmd:
        out.append(
            f"- **Отклик на команду оператора** — p50 {percentile(cmd.samples, 50):.0f} мс, "
            f"p95 {cmd.p95:.0f} мс при нормативе {cmd.target:.0f} мс. "
            f"{'Норматив выдержан' if cmd.ok else 'Норматив не выдержан'}. "
            f"Худший случай — {max(cmd.samples):.0f} мс: это такты, на которых "
            "команда попадает в момент рассылки телеметрии и записи её в БД, "
            "потому что запись идёт в том же цикле событий.")

    tick = by.get("Период выдачи телеметрии")
    if tick:
        drift = tick.p95 - tick_period * 1000
        out.append(
            f"- **Период выдачи телеметрии** — p95 {tick.p95:.1f} мс при такте "
            f"{tick_period * 1000:.0f} мс, отклонение {drift:+.1f} мс. Такт "
            "отсчитывается от расписания, а не от конца обработки, поэтому "
            "задержка не накапливается: за получасовую тренировку модельное "
            "время не расходится с реальным.")

    long_debrief = by.get("GET /api/sessions/{id}/debrief (длинная)")
    if long_debrief:
        out.append(
            f"- **Разбор тренировки** открывается за {long_debrief.p95:.0f} мс (p95) "
            "на получасовой тренировке — это один запрос, отдающий сразу журнал, "
            "телеметрию, ошибки и оценку.")

    login = by.get("Вход в систему (Argon2id)")
    if login:
        out.append(
            f"- **Вход в систему** — {login.p95:.0f} мс (p95). Это цена Argon2id: "
            "стойкость к перебору покупается временем счёта. На отклик рабочего "
            "места не влияет — вход выполняется один раз за смену.")

    eng = by.get("Шаг матмодели (без сети и БД)")
    if eng and eng.p95 > 0:
        ratio = f"{round(tick_period * 1000 / eng.p95, -3):,.0f}".replace(",", " ")
        out.append(
            f"- **Запас цифрового двойника** — шаг матмодели {eng.p95:.3f} мс, "
            f"это примерно в {ratio} раз быстрее такта. "
            "Замена заглушек на реальные модели ректификации и теплообмена "
            "упирается не в такт симуляции.")

    failed = [m.name for m in metrics if m.ok is False]
    out.append("")
    out.append(f"**Итог:** нормативы {'выдержаны по всем показателям' if not failed else 'превышены: ' + ', '.join(failed)}.")
    return out


def report(metrics: list[Metric], meta: dict, tick_period: float) -> str:
    lines = ["# Время отклика и задержки КТК ЭЛОУ-АВТ", "",
             "Снято скриптом `scripts/measure_latency.py`, значения — миллисекунды.",
             "Файл перевыпускается командой:", "",
             "```bash", "python scripts/measure_latency.py --out docs/performance.md", "```", ""]
    lines += ["## Условия замера", ""]
    lines += [f"- **{k}** — {v}" for k, v in meta.items()]
    lines += ["", "## Результаты", "", TABLE_HEADER]
    lines += [m.row() for m in metrics]
    lines += ["", "## Выводы", ""]
    lines += conclusions(metrics, tick_period)
    return "\n".join(lines) + "\n"


async def run(args) -> None:
    proc = db = None
    url = args.url
    if not url:
        proc, url, db = start_server(args.tick)
    try:
        await wait_ready(url)
        login_m, cookie = await measure_login(url, args.login, args.password, args.logins)
        if not cookie:
            raise RuntimeError("сервер не выдал cookie сеанса")

        metrics = [login_m]
        async with httpx.AsyncClient(base_url=url,
                                     cookies={"ktk_session": cookie}) as c:
            metrics.append(await measure_rest(c, "/api/me", "GET /api/me", args.rest,
                                              "проверка сеанса, каждая загрузка страницы",
                                              target=TARGETS["rest"]))
            metrics.append(await measure_rest(c, "/api/scenarios", "GET /api/scenarios",
                                              args.rest, "список сценариев",
                                              target=TARGETS["rest"]))
            metrics.append(await measure_rest(c, "/api/sessions", "GET /api/sessions",
                                              args.rest, "список тренировок",
                                              target=TARGETS["rest"]))

            cmd_m, tick_m, sid = await measure_ws(url, cookie, args.scenario,
                                                  args.commands, args.ticks, args.tick)
            metrics += [cmd_m, tick_m]

            points = len((await c.get(f"/api/sessions/{sid}/telemetry")).json())
            metrics.append(await measure_rest(
                c, f"/api/sessions/{sid}/debrief", "GET /api/sessions/{id}/debrief",
                args.rest, f"разбор целиком, точек телеметрии: {points}",
                target=TARGETS["rest"]))

            # тот же разбор, но на объёме получасовой тренировки
            if db and args.long_points:
                inflate_telemetry(db, sid, args.long_points - points)
                total = len((await c.get(f"/api/sessions/{sid}/telemetry")).json())
                metrics.append(await measure_rest(
                    c, f"/api/sessions/{sid}/debrief",
                    "GET /api/sessions/{id}/debrief (длинная)", max(args.rest // 4, 20),
                    f"тренировка ~{total // 60} мин, точек телеметрии: {total}",
                    target=TARGETS["rest"]))

        metrics.append(measure_engine_step(args.steps))

        meta = {
            "Дата": time.strftime("%Y-%m-%d %H:%M:%S"),
            "Платформа": f"{platform.system()} {platform.release()}, {platform.machine()}",
            "Python": platform.python_version(),
            "Процессор": f"{os.cpu_count()} логических ядер",
            "База данных": ("SQLite (временный файл)" if db
                            else "как настроена на стенде (см. DATABASE_URL)"),
            "Сеть": "клиент и сервер на одной машине, сетевая задержка не входит в замер",
            "Такт симуляции": f"{args.tick * 1000:.0f} мс",
            "Разброс": ("замеры сняты на машине, где выполняются и другие задачи; "
                        "от прогона к прогону p95 отклика гуляет в разы, поэтому "
                        "сравнивать имеет смысл с нормативом, а не два прогона между собой"),
        }
        text = report(metrics, meta, args.tick)
        print(text)
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            print(f"[записано] {out}")
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if db:
            db.unlink(missing_ok=True)


def main() -> None:
    p = argparse.ArgumentParser(description="Замер времени отклика и задержек КТК")
    p.add_argument("--url", help="адрес поднятого стенда; без него сервер поднимается сам")
    p.add_argument("--login", default="operator")
    p.add_argument("--password", default="operator")
    p.add_argument("--scenario", default="pressure_alarm",
                   help="сценарий тренировки для замера (по умолчанию с аварией)")
    p.add_argument("--rest", type=int, default=200, help="замеров на каждый REST-запрос")
    p.add_argument("--logins", type=int, default=20, help="замеров входа в систему")
    p.add_argument("--commands", type=int, default=200, help="замеров отклика на команду")
    p.add_argument("--ticks", type=int, default=30, help="замеров периода телеметрии")
    p.add_argument("--steps", type=int, default=20000, help="замеров шага матмодели")
    p.add_argument("--long-points", type=int, default=1800,
                   help="объём телеметрии для замера разбора длинной тренировки "
                        "(1800 точек ≈ 30 мин); 0 — не мерить")
    p.add_argument("--tick", type=float, default=1.0,
                   help="такт симуляции, с (штатный — 1.0)")
    p.add_argument("--out", help="куда записать отчёт, например docs/performance.md")
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
