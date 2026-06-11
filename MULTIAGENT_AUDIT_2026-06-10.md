# Мультиагентный аудит — book.pashynskaphoto.com
**Дата:** 2026-06-10 · **Метод:** 5 агентов → круглый стол → план · **База:** 333 passed, 1 skipped

> Контекст: утренний `AUDIT_REPORT_2026-06-10.md` уже закрыл базовую безопасность (SQLi, admin auth, webhook signatures, CSP/HSTS, CVE, git-секреты). Этот аудит копает глубже — бизнес-логика, конверсия, деньги, автоматизация.

---

## Что уже отлично — НЕ ТРОГАТЬ

- **Анти-двойное-бронирование:** `BEGIN IMMEDIATE` + глобальная проверка date+time между событиями (app.py:4455) + sweep каждые 30 сек.
- **e-Transfer matching v2:** точная сумма, ambiguity → ручной разбор + алерт, partial payments, reconciliation 45 дней, admin/transfers UI.
- **Безопасность:** все 54 admin-роута под `@admin_required` (проверено скриптом — 0 дыр), webhooks fail-closed, `/confirm` и `/cancel-reservation` под токеном, reCAPTCHA + honeypot.
- **UX-база:** testimonials с фото, 4.9★/145+, копи-пилюли и таймер на payment-странице, autocomplete/inputmode на полях, 4 языка. Instagram и телефон уже необязательны.
- **Автоматизация:** abandoned recovery (2ч), напоминания 48ч/24ч, review-письмо (5д), AI-ассистент сайт+Telegram, n8n event bus, funnel-аналитика.

---

## Фаза 1 — находки агентов

### 🎨 Agent #1 UX
| # | Находка | Где |
|---|---------|-----|
| U1 | **Прошедшие слоты бронируемы в день съёмки**: в 15:00 клиент видит и бронирует слот 10:00. Ни сервер, ни фронт не фильтруют прошедшее время | app.py:3998–4058 (`/slots`), 4405 (`/reserve`) |
| U2 | **Вечером rolling-даты «сегодня» отклоняются как past**: сервер в UTC, после ~18:00 Calgary `datetime.now().date()` = завтра | app.py `_rolling_date_unavailable_reason` |
| U3 | Бейдж «In X days» врёт на 1 вечером (та же UTC-причина) | app.py:3500 |

### ⚙️ Agent #2 Backend
| # | Находка | Где |
|---|---------|-----|
| B1 | **Naive `datetime.now()` по всему коду + контейнер Fly в UTC** (TZ нигде не задан). Источник U1–U3 + окна напоминаний съезжают на ~6–7ч | fly.toml, Dockerfile, app.py повсюду |
| B2 | **`db_conn()` без WAL и busy_timeout**: 4 gthread-потока + 2 daemon-потока пишут конкурентно; дефолтные 5с timeout — на грани | app.py:3258 |
| B3 | Масштабирование на 2+ машины Fly задвоит письма/watcher (`--workers 1` — негласный инвариант) | start.sh:114 |

### 🔒 Agent #3 Security
| # | Находка | Где |
|---|---------|-----|
| S1 | **`/waitlist` без rate limit**: спам → флуд в Telegram + мусор в таблице (INSERT без дедупа) | app.py:4231 |
| S2 | `?key=` для admin API течёт в логи/историю (перенесено из утреннего аудита, всё ещё открыто) | app.py:527 |
| S3 | CSP `'unsafe-inline'` для скриптов (перенесено, LATER) | app.py:1718 |

### 💰 Agent #4 Booking Auditor
| # | Находка | Где |
|---|---------|-----|
| P1 | **Лист ожидания мёртвый груз**: при отмене/истечении слота никто из waitlist не уведомляется — упущенная выручка на sold-out датах | app.py: hooks отсутствуют в sweep / `/cancel-reservation` / `/admin/cancel` |
| P2 | **24ч-напоминание не содержит остаток к оплате**: баланс просят вручную в день съёмки (`/admin/request-balance`) — трение + риск no-show | app.py:1455 (`_send_24h_reminder_email`) |
| P3 | e-Transfer пришёл после грейса (15м + 60м), клиент не нажал «I sent»: бронь истекла, платёж лёг unmatched. Алерт есть, но восстановление руками | check_etransfer_v2.py:386 |

### 🤖 Agent #5 AI Strategist
Гиммиков не предлагаем. Реальные кандидаты: waitlist-автоуведомление (=P1), баланс в 24ч-письме (=P2), второе abandoned-касание через ~20ч **с проверкой, что слот всё ещё свободен** (инфра `_process_abandoned_emails` уже готова). Ассистент и review-helper уже закрывают остальное — дублировать нечего.

---

## Фаза 2 — круглый стол: TOP-5

| Место | Проблема | Голоса |
|-------|----------|--------|
| 🔴 1 | Timezone-корректность + отсечка прошедших слотов (U1+U2+U3+B1) | #1 #2 #4 |
| 🟠 2 | Waitlist-автоуведомление при освобождении слота (P1) | #1 #4 #5 |
| 🟠 3 | Баланс + ссылка на оплату в 24ч-напоминании (P2) | #1 #4 #5 |
| 🟡 4 | SQLite: WAL + busy_timeout (B2) | #2 |
| 🟡 5 | Rate limit на `/waitlist` (S1) | #3 |

---

## Фаза 3 — план действий

### 🔥 NOW (сегодня, ~2–3 часа суммарно)

**1. Таймзона + прошедшие слоты** — `app.py`
- Добавить рядом с `CALENDAR_TZ` (строка 2404):
  ```python
  from zoneinfo import ZoneInfo
  BOOKING_TZ = ZoneInfo(os.environ.get("BOOKING_TZ", "America/Edmonton"))
  def now_local():
      return datetime.now(BOOKING_TZ).replace(tzinfo=None)
  ```
  (naive-локальное время — остальной код сравнения строк ISO продолжает работать без изменений).
- Заменить `datetime.now()` → `now_local()` в: `/slots` (3999), `/reserve` (4445), `_rolling_date_unavailable_reason`, `_enrich_event_for_landing` (3500), `_process_abandoned/reminder/24h/review_emails` (3104–3221), `/confirm` (4712). **Не трогать** места с `datetime.now(timezone.utc)` — они уже корректны.
- В `/slots` после `booked_times` добавить отсечку: если `booking_date == now_local().date().isoformat()` — убрать слоты с `time <= now_local().strftime("%H:%M")` (опционально +30 мин буфер).
- В `/reserve` то же правило перед INSERT (server-side обязательно, фронт — опционально).
- **Тест:** `tests/test_timezone_slots.py` — monkeypatch `now_local`; кейсы: вечерний rolling «сегодня» проходит; прошедший слот не выдаётся и отклоняется в `/reserve`; завтрашние не задеты.

**2. WAL + busy_timeout** — `app.py:3258`
```python
def db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn
```
- **Тест:** весь suite зелёный; smoke: 2 потока × 50 параллельных `/reserve` без `database is locked`.

**3. Rate limit `/waitlist`** — `app.py:4231`
- Скопировать блок IP+`check_rate_limit` из `/reserve` (4437–4443) в начало `join_waitlist`; плюс дедуп: `INSERT ... ON CONFLICT` или предварительный SELECT по (event_id, email) → «You're already on the waitlist».
- **Тест:** в `tests/test_waitlist.py` — 6-й запрос подряд → 429; повторный email → без дубля и без второго TG-алерта.

### 📅 NEXT (эта неделя)

**4. Waitlist-автоуведомление** — `app.py`
- Хелпер `_notify_waitlist_slot_freed(event_id, date, time)`: выбрать из `waitlist` по event_id (LIMIT 10, исключив уже забронировавших email), отправить письмо «Слот {time} освободился — забронировать: {ссылка}» + пометить `notified_at` (ALTER TABLE waitlist ADD COLUMN notified_at TEXT — в `_migrate`).
- Вызвать из 3 мест: sweep истечений в `_watcher_thread`, `/cancel-reservation`, `/admin/cancel`. First-come-first-served, без резервирования за waitlist-клиентом (просто гонка по ссылке — честно и просто).
- **Тест:** отмена брони → mock-письма ушли только не-уведомлённым.

**5. Баланс в 24ч-письме** — `app.py:1455`
- Вынести из `/admin/request-balance` (7019) создание Stripe-ссылки в хелпер `_create_balance_checkout(booking)`; в `_send_24h_reminder_email` добавить блок: «Остаток: $X — картой заранее {stripe_url} или e-Transfer/наличными на съёмке». Если Stripe недоступен — письмо уходит без ссылки (try/except, как в admin_confirm).
- **Тест:** письмо содержит верный остаток (total+addons−paid_amount); при paid ≥ total блок отсутствует.

**6. Второе abandoned-касание** — `app.py:3104`
- Колонка `abandoned_email2_sent`; в `_process_abandoned_emails` вторая выборка: expired 18–28ч назад, слот **всё ещё свободен** (проверка по bookings) → короткое письмо «слот ещё ваш».
- **Тест:** слот занят другим → письмо не уходит.

### 🌙 LATER

- `?key=` → header-only после миграции n8n/cron (S2).
- CSP nonce вместо `'unsafe-inline'` (S3).
- Гард от двух машин Fly: `fly scale count 1` зафиксировать в DEPLOY_SOURCE_OF_TRUTH.md; либо advisory-lock в /data (B3).
- Late e-Transfer: кнопка «восстановить бронь» прямо в ambiguity-алерте Telegram (P3).
- Рефакторинг app.py (9540 строк) на модули — только при следующем большом изменении, не ради красоты.

### ❌ Чего НЕ делать
Не переходить на Postgres, не вводить Celery/Redis (1 воркер тянет), не переписывать e-Transfer matching (v2 продуман), не добавлять AI-чат-апселлы. Система зрелая — точечные правки, не реконструкция.

---
*Каждый NOW/NEXT-пункт — отдельный коммит с тестом. После NOW: `python3 -m pytest tests/ -q` → зелёный, затем деплой по DEPLOY_SOURCE_OF_TRUTH.md.*
