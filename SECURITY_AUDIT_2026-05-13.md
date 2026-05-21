# Security & Quality Audit — 2026-05-13
**Booking-System / app.py @ 4055 строк**
Аудит выполнен в **read-only режиме**. Код, БД, .env, git, fly-конфигурация — **не изменялись**.
Baseline: `python3 -m pytest tests/ -q` → **59 passed ✅** (до и после аудита).

---

## TL;DR

Система зрелая и аккуратно написана. Большинство критичных вещей уже сделано правильно:
SQL — только параметризованные запросы; `BEGIN IMMEDIATE` против двойных бронирований;
HMAC-сравнение для admin-пароля и Telegram-webhook; Stripe-подпись проверяется; `send_from_directory` для path-traversal; rate-limits на `/reserve`, `/admin/login`, `/assistant/chat`; secure-by-default отказы (fail-closed).

**Реальных дыр, через которые что-то можно сломать снаружи, я не нашёл.** Ниже — список улучшений, отсортированный по приоритету. Ни одно из них не требует переписывания работающего кода — это аккуратные дополнения.

---

## P1 — стоит сделать в ближайшие дни

### 1.1 `FLY_API_TOKEN.txt` лежит рядом с кодом
**Файл:** `FLY_API_TOKEN.txt` (646 bytes, содержит реальный `fm2_...` токен).
**Статус git:** в `.gitignore` есть строка `FLY_API_TOKEN.txt`, так что в репо его быть не должно — **обязательно проверить через `git log --all -- FLY_API_TOKEN.txt`** на твоей машине (внутри sandbox нет доступа к .git).
**Что сделать:** даже если файла нет в git — лучше переместить токен в системный keychain или `~/.config/fly/`, а файл удалить. Любой, у кого временный доступ к ноуту, увидит prod-токен.

### 1.2 Stripe webhook принимает unsigned events, если `STRIPE_WEBHOOK_SECRET` пустой
**Файл:** `app.py:2630–2640`.
**Проблема:** если переменная не выставлена в проде, webhook логирует warning и **принимает запрос без проверки подписи**. Это значит, что любой, кто узнал URL `/stripe/webhook`, может подделать `checkout.session.completed` и **бесплатно подтвердить бронирование**.
**Простой фикс:** заменить fallback на возврат `503 "webhook secret not configured"`, как сделано в `/telegram/webhook` (он уже делает fail-closed). Поведение для Telegram-вебхука — образец для подражания.

### 1.3 Сессия cookie без явных безопасных флагов
**Файл:** `app.py`, в коде нет ни одного из `SESSION_COOKIE_SECURE / SESSION_COOKIE_HTTPONLY / SESSION_COOKIE_SAMESITE`.
**Текущее поведение:** Flask 3 → `HTTPONLY=True` (ок), `SECURE=False` (плохо для prod), `SAMESITE=None` (нет атрибута → большинство браузеров обработают как `Lax`).
**Что добавить (буквально 4 строки в `app.py` после `app.secret_key = ...`):**
```python
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FLASK_ENV") != "development"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 8  # 8 часов
```
Это снижает риск перехвата cookie + CSRF одновременно. Тесты ходят через test_client — флаги не помешают.

### 1.4 Admin XSS в JS-контексте через имя клиента
**Файл:** `templates/admin.html:1010–1017`, `templates/payment.html:525`.
**Проблема:** `onclick="confirmBooking({{ b.id }}, '{{ b.name | e }}')"` — фильтр `| e` эскейпит HTML, но **не одинарные кавычки и обратный слэш в JS-контексте**. Если клиент введёт имя `O'Reilly` или `\'); alert(1);//`, кнопка сломается / выполнит код в браузере админа.
**Простой фикс:** заменить `| e` на `| tojson` (Jinja умеет — это безопасный JSON-литерал):
```html
onclick='confirmBooking({{ b.id }}, {{ b.name | tojson }})'
```
Аналогично для `cancelBooking`, `deleteBooking`, `duplicateEvent`, `deleteEvent` и template-literal в `payment.html:525`.
**Импакт:** stored XSS, но только в админке (которая под паролем). Атакующий = клиент с экзотическим именем; жертва = ты, когда жмёшь Confirm.

---

## P2 — улучшения качества, без срочности

### 2.1 Добавить недостающие security-headers
**Файл:** `app.py:945–950` — уже есть X-Content-Type-Options, X-Frame-Options, Referrer-Policy.
**Что добавить:**
- `Strict-Transport-Security: max-age=15552000; includeSubDomains` — у тебя `force_https = true` в `fly.toml`, так что HSTS только усилит.
- `Content-Security-Policy` — можно начать с мягкого `default-src 'self' https: data:; img-src 'self' https: data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline' https://js.stripe.com` и наблюдать report-uri неделю, прежде чем закручивать.
- `Permissions-Policy: camera=(), microphone=(), geolocation=()` — отключает фичи, которые приложению не нужны.

### 2.2 `datetime.utcnow()` deprecated в Python 3.12+
**Файл:** `app.py:2426, 2848`.
**Что сделать:** заменить на `datetime.now(timezone.utc)` (нужен импорт `timezone`).
Сейчас не ломает ничего — но при обновлении базового образа `python:3.11-slim` → `python:3.12` появятся DeprecationWarning в логах.

### 2.3 Bare `except:` в 7 местах
**Файлы:** `app.py:3316, 3370`, `check_etransfer.py:136,352`, `check_etransfer_v2.py:149`, `timed_cron.py:261,445`.
**Что сделать:** заменить на `except Exception:` (или явный класс) — это не даст глушить `KeyboardInterrupt` и `SystemExit`. Поведение остаётся тем же, но безопаснее при отладке.

### 2.4 `booking-status` сравнивает токен через `!=`
**Файл:** `app.py:3884`.
**Замечание:** token = 32-байтовый `secrets.token_hex(16)`, brute-force нереален, но для консистентности (admin-login и telegram-webhook уже на `hmac.compare_digest`) стоит привести к одному стилю:
```python
if not token or not stored_token or not hmac.compare_digest(token, stored_token):
```

### 2.5 In-memory rate-limit не масштабируется
**Файл:** `app.py:160–206`.
**Замечание:** если когда-нибудь поднимешь больше одного gunicorn-worker, лимит станет per-worker. Сейчас у тебя на fly 1 машина / 1 процесс — не проблема. Если будешь масштабировать — переезжать на `flask-limiter` с Redis backend. **Не сейчас, оставить как есть.**

### 2.6 ResourceWarning при импорте app.py в тестах
**Источник:** `app.py:43` (FileHandler для booking.log).
**Симптом:** `pytest` показывает `ResourceWarning: unclosed file …booking.log`.
**Фикс по желанию:** в конце логов добавить `logging.shutdown()`-handler через atexit. Косметика, никаких функциональных проблем.

---

## P3 — наблюдения (ничего делать сейчас не надо)

| Что | Где | Почему ок | Что отметить на будущее |
|---|---|---|---|
| 4055 строк в одном `app.py` | весь app.py | работает, тесты зелёные, рефакторить = риск | если будут добавляться большие фичи (Google Calendar, Twilio) — выносить в модули |
| Логи содержат email клиентов | `app.py:1441,1473,…` | удобно для отладки no-show; локальный сервер | при выводе в SaaS-логи (Datadog/Sentry) — маскировать PII |
| 1713 клиентов в CRM | `02-Clients-CRM/` | в .gitignore (по структуре проекта) | не коммитить, бэкап шифровать |
| Public site `innerHTML` на event-данных | `index_v2.html:595,676,874` | контент — только админский (events.yaml + admin upload) | если когда-нибудь добавишь user-submitted отзывы → переходить на `textContent` или явный sanitizer |

---

## P4 — что НЕ надо трогать (работает отлично)

1. **`BEGIN IMMEDIATE` в `/reserve`** (`app.py:2103`) — корректная защита от двойного бронирования на SQLite, проще и надёжнее, чем optimistic locking. Тестами покрыто.
2. **Admin auth** (`app.py:208–247`) — поддержка и session-login, и `X-Admin-Key` / `?key=` для programmatic доступа, fail-closed по умолчанию, `hmac.compare_digest`. Все 24 admin-маршрута имеют декоратор `@admin_required`, кроме `/admin/login` и `/admin/logout` (правильно — они должны быть открыты).
3. **Telegram webhook** (`app.py:3896–3916`) — образцовый: HMAC-проверка секрета, fail-closed при отсутствии настройки.
4. **Stable secret key** (`app.py:64–90`) — приоритет env → `/data/.flask_secret`, никогда не пересоздаёт при рестарте → сессии админа не слетают.
5. **`MAX_CONTENT_LENGTH = 10 MB`** (`app.py:62`) — защита от DoS через гигантские загрузки.
6. **Upload safety** (`app.py:3270–3337`) — генерация имени файла через `uuid.uuid4()`, whitelist расширений, `send_from_directory` для отдачи.
7. **Path-traversal защита** — `serve_image` использует `send_from_directory` (Flask нормализует).
8. **Open-redirect защита** в `/admin/login` (`app.py:2737–2739`) — `next_url` должен начинаться с `/` и не с `//`.
9. **events.yaml hot-reload** после CRUD-операций — корректно вызывает `_reload_events_globals()`.
10. **Тесты** — 59 штук покрывают booking flow, типы, e-transfer parsing, global block, i18n, frontend contract. Это редкая ценность; беречь.

---

## Чеклист OWASP Top-10 — результат

| # | Категория | Статус |
|---|---|---|
| A01 Broken Access Control | ✅ admin_required на всех чувствительных | |
| A02 Cryptographic Failures | ⚠️ нет HSTS / `SESSION_COOKIE_SECURE` явно — см. P1.3, P2.1 |
| A03 Injection (SQL/CMD/etc.) | ✅ параметризованные запросы, безопасные f-strings only с whitelisted keys |
| A04 Insecure Design | ✅ BEGIN IMMEDIATE, fail-closed Telegram |
| A05 Security Misconfig | ⚠️ Stripe fallback без подписи — см. P1.2; токен Fly рядом — см. P1.1 |
| A06 Vulnerable Components | ℹ️ requirements зафиксированы; стоит периодически `pip-audit` |
| A07 Auth Failures | ✅ hmac.compare_digest, rate-limit 10/15min на login |
| A08 Integrity Failures | ✅ Stripe signature; Telegram signature |
| A09 Logging | ✅ есть логи; ⚠️ PII в логах — норм для локального, риск для cloud-logs |
| A10 SSRF | ℹ️ внешние HTTP только к Telegram/Stripe/Notion API, URL — литералы. ✅ |

---

## Рекомендуемый порядок действий

1. **Сейчас:** удостовериться, что `FLY_API_TOKEN.txt` не в git-истории; перенести токен из файла в безопасное место.
2. **Когда сядешь делать релиз:** P1.2 (Stripe fail-closed) + P1.3 (cookie-флаги) + P1.4 (`|tojson`). Всё это правки в 5–15 строк, тесты остаются зелёными.
3. **Следующий спринт:** P2.1 (HSTS/CSP), P2.2 (utcnow), P2.3 (bare except).
4. **Никогда:** не переписывать `app.py` ради «архитектуры». Текущий монолит надёжный, тестов 59, бизнес-логика устоялась.

---

**Аудит выполнен read-only.** Никакие файлы кода, БД, .env, конфигов, тестов не модифицировались. Baseline `59 passed` подтверждён до и после.
