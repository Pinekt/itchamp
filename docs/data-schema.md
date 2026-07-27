# Схема данных КТК ЭЛОУ-АВТ

База: **ktk_eloyavt** (PostgreSQL). 9 таблиц. Каждая закрывает конкретный
критерий оценки кейса — это аргумент для презентации, а не просто хранилище.

| Таблица | Назначение | Критерий |
|---|---|---|
| `users` | пользователи и роли: operator / instructor / admin | ИБ (разграничение доступа), тех. реализация (разделение ролей) |
| `audit_log` | аудит действий пользователей (вход, старт сессии, …) | ИБ (мониторинг и аудит) |
| `scenarios` | учебные сценарии: начальное состояние + отказы | тех. реализация, демонстрация |
| `scenario_steps` | **эталонная** последовательность действий | ИИ (сравнение с эталоном) |
| `training_sessions` | сессии тренировок: кто, когда, какой сценарий | демонстрация, оценка квалификации |
| `operator_actions` | журнал действий + **время реакции** | тех. реализация (журнал, время) |
| `telemetry` | история параметров установки по секундам | демонстрация, разбор с инструктором |
| `detected_errors` | выход ИИ: класс, локализация, объяснение, риск | ИИ (классификация и локализация) |
| `assessments` | итог: баллы, вердикт, среднее время реакции | оценка квалификации персонала |

## Связи

```
users ──< training_sessions ──< operator_actions
                │              ──< telemetry
                │              ──< detected_errors
                └────────────────< assessments
scenarios ──< scenario_steps
users ──< audit_log
```

## Методика оценки (черновая, уточняет Константин)

100 баллов минус штрафы: критическая ошибка −25, некритическая −8.
Порог сдачи — 70 баллов. Дополнительно считаются: количество ошибок по классам,
среднее время реакции, число действий. Всё сохраняется в `assessments.details`.

## Начальные данные (создаются автоматически)

- Пользователи: `operator`, `instructor`, `admin`. Пароль по умолчанию равен
  логину и переопределяется переменными `KTK_PASSWORD_OPERATOR` и т. п.
  Хранится **хеш Argon2id** (`users.password_hash`, вид `$argon2id$v=19$...`);
  старые SHA-256-хеши распознаются и автоматически переводятся в Argon2id
  при первом успешном входе.
- Сценарии: `startup` (пуск), `pump_trip` (отказ насоса), `pressure_alarm` (рост давления)
  — с эталонными шагами для каждого.

## События журнала аудита

`audit_log.event` — справочник значений (аргумент по критерию ИБ):

| Событие | Когда возникает | Что в `details` |
|---|---|---|
| `login_success` | успешный вход | логин, роль, User-Agent |
| `login_failed` | неверный пароль или логин | логин, номер попытки, User-Agent |
| `login_blocked` | сработала защита от подбора | логин, сколько секунд ждать |
| `logout` | выход из системы | логин |
| `access_denied` | не хватило прав | путь запроса, роль, что требовалось |
| `password_rehashed` | пароль переведён на Argon2id | алгоритм |
| `session_start` | начата тренировка | id тренировки, сценарий |
| `session_end` | тренировка завершена | id тренировки, статус |

У всех событий заполняются `user_id` (кроме блокировки до опознания
пользователя) и `ip` — с учётом заголовка `X-Forwarded-For`, если
приложение стоит за обратным прокси.

## Полезные запросы

```sql
-- кто и откуда входил в систему за последние сутки
SELECT a.created_at, u.login, a.event, a.ip
FROM audit_log a LEFT JOIN users u ON u.id = a.user_id
WHERE a.event IN ('login_success','login_failed','login_blocked')
ORDER BY a.created_at DESC;

-- попытки доступа к чужим данным
SELECT u.login, a.details, a.ip, a.created_at
FROM audit_log a JOIN users u ON u.id = a.user_id
WHERE a.event = 'access_denied' ORDER BY a.created_at DESC;

-- последние тренировки с оценками
SELECT s.scenario_code, s.operator, a.total_score, a.verdict, a.errors_count
FROM training_sessions s LEFT JOIN assessments a ON a.session_id = s.id
ORDER BY s.started_at DESC LIMIT 10;

-- какие ошибки оператор допускает чаще всего
SELECT error_class, COUNT(*) FROM detected_errors GROUP BY error_class ORDER BY 2 DESC;

-- среднее время реакции по сценариям
SELECT s.scenario_code, ROUND(AVG(a.reaction_ms)) AS avg_ms
FROM operator_actions a JOIN training_sessions s ON s.id = a.session_id
GROUP BY s.scenario_code;

-- эталон против факта: что оператор должен был сделать
SELECT st.order_no, st.expected_action, st.description, st.critical
FROM scenario_steps st JOIN scenarios sc ON sc.id = st.scenario_id
WHERE sc.code = 'pump_trip' ORDER BY st.order_no;
```
