# Аудит и оптимизация — book.pashynskaphoto.com
**Дата:** 2026-06-10 · **Исполнитель:** Claude Fable 5 · **Ветка:** main (8 коммитов, без push/деплоя)

---

## 1. Краткое резюме

Кодовая база в заметно лучшем состоянии, чем предполагал чеклист хэндбука: SQL-инъекций нет (везде параметризованные запросы), admin-роуты закрыты декоратором с timing-safe сравнением и rate-limit, двойные брони исключены (`UNIQUE(date,time)` + `BEGIN IMMEDIATE`), Stripe webhook проверяет подпись, security-заголовки (CSP/HSTS/XFO) на месте. Главные реальные проблемы были другими:

1. ⚡ **Пароль админки лежал в git** (`.env.qa`, `ADMIN_PASSWORD=Hawanj100`) — и это же значение, судя по всему, действующий `ADMIN_KEY`. Файл больше не отслеживается, но **история git его помнит — пароль и ключ нужно сменить** (см. §4).
2. ⚡ **pytest ходил в боевой Stripe**: `.env` содержит live-ключ (`rk_live…`), который тесты подхватывали и создавали реальные checkout-сессии. Теперь тестовое окружение герметично.
3. ⚡ **Откат admin.html стёр фичи**: восстановление после обрезанного файла (8f87f8e) взяло старую версию — пропали редактор аддонов, batch-загрузка фото и последовательное сохранение (вернулась гонка записи events.yaml, способная молча откатывать цены). Всё восстановлено + сохранена кнопка приватных сессий.
4. ⚡ **Ваш незакоммиченный WIP ронял подтверждение брони**: не обёрнутый вызов Stripe в `admin_confirm` давал 500 при недоступности Stripe; плюс TypeError при `full_price=NULL`. Исправлено, WIP сохранён отдельным коммитом.
5. 🟡 **10 CVE в зависимостях** (flask, requests, python-dotenv, Pillow ×6) — обновлены, `pip-audit` чист, все тесты зелёные.

**Итог: 326 passed, 1 skipped, 0 failed** (было 6 падений и 2 скрытых бага). Хэндбук устарел: тестов 327, а не 59.

---

## 2. Список багов и уязвимостей

### ⚡ Критические (исправлено в этой сессии)
| # | Проблема | Где | Фикс |
|---|----------|-----|------|
| 1 | Пароль админки в git-истории (`.env.qa`) | git tracking | `git rm --cached`, `.env.qa.example`, .gitignore `​.env.*`; **ротация — на вас, §4** |
| 2 | Тесты создают live-mode Stripe Checkout-сессии | tests/conftest.py | env-переменные обнуляются до импорта app |
| 3 | Потерянные фичи admin-панели + гонка записи events.yaml (Promise.all) | templates/admin.html | восстановлен из 2a2bf92 + патч приватных сессий |
| 4 | `/admin/confirm` → 500 при сбое Stripe; TypeError при NULL full_price (WIP) | app.py admin_confirm | try/except + None-safe хелперы `_booking_*` |

### 🟡 Высокий/средний (исправлено)
| # | Проблема | Фикс |
|---|----------|------|
| 5 | CVE: flask 3.0.3, requests 2.32.3, python-dotenv 1.0.1, Pillow 12.1.1 | → 3.1.3 / 2.33.0 / 1.2.2 / 12.2.0; pip-audit: 0 vulns |
| 6 | `debug=True` захардкожен при `python app.py` на 0.0.0.0 (Werkzeug RCE) | debug только при `FLASK_DEBUG=1` |
| 7 | Локальный `.env`: SECRET_KEY-плейсхолдер «generate…» + дубль ADMIN_PASSWORD | сгенерирован настоящий ключ, дубль убран (бэкап: `/tmp/env_backup_20260610` в сэндбоксе; локальные dev-сессии разлогинятся) |
| 8 | booking.db / qa/booking.db в git (0 байт, но прецедент опасный — в проде 1713 клиентов) | untracked |

### 🟡 Требует ваших действий (я не имею доступа)
| # | Проблема | Действие |
|---|----------|----------|
| 9 | Утёкший пароль = текущий `ADMIN_KEY` | §4, ротация на Fly и в `.env` |
| 10 | Не проверено, задан ли `FLASK_SECRET_KEY` в Fly secrets (если нет — сессии подписываются автогенерированным файлом `/data/.flask_secret`, это ок, но проверьте: `flyctl secrets list`) | проверить |

### 🟢 Низкий приоритет (не исправлял — план в §3)
- `?key=` query-param для admin API: ключ попадает в логи доступа и историю браузера. Оставил (мог сломать внешние интеграции n8n/cron), но рекомендую перейти на header-only.
- CSP содержит `'unsafe-inline'` для скриптов — переход на nonce потребует правки всех шаблонов.
- `app.py` = 9526 строк, 106 роутов — рефакторинг на модули (booking/payments/admin) сознательно отложен.
- Мусор в корне: `.last-known-good.py` (240KB), `*.log`, старые `*.bak`, десяток разовых MD-отчётов — предлагаю разнести по `docs/` и `backups/`.
- `events.yaml` потенциально редактируется параллельно админкой и cron — фронтенд-гонку убрали, но файловая блокировка на бэкенде была бы надёжнее.

---

## 3. План улучшений (пошагово)

**Шаг 1 — сегодня (15 мин, вы):** ротация секретов по §4, затем `flyctl deploy` с обновлёнными зависимостями.
**Шаг 2 — на этой неделе (~1 ч):** `git filter-repo --invert-paths --path .env.qa` для очистки истории (если репо когда-либо покидало ваш Mac — обязательно; если строго локально — достаточно ротации). Перейти на header-only admin-ключ, предварительно проверив n8n-сценарии.
**Шаг 3 — следующий спринт (~3-4 ч):** разбить `app.py` на blueprints (booking, payments, admin, integrations) — тесты уже дают сетку безопасности; добавить type hints в новые модули; mypy в CI.
**Шаг 4 — фоном:** CSP-nonce вместо `unsafe-inline`; файловый лок на events.yaml; Lighthouse-прогон после деплоя (статика уже с длинным кешем из 2a2bf92); автонапоминание об остатке оплаты — ваш WIP уже закладывает фундамент (кнопка в письме готова).

---

## 4. 🔑 Ротация секретов — сделать вам (5 минут)

```bash
# 1. Новые значения
python3 -c "import secrets; print('ADMIN_PASSWORD=', secrets.token_urlsafe(16))"
python3 -c "import secrets; print('ADMIN_KEY=', secrets.token_urlsafe(24))"

# 2. Прод (Fly)
flyctl secrets set ADMIN_PASSWORD='<новый>' ADMIN_KEY='<новый>' -a iryna-booking

# 3. Локально — обновить те же значения в .env
# 4. Если ADMIN_KEY используется в n8n — обновить и там
```

## 5. Коммиты сессии (main, не запушено)

```
00a079e security: Werkzeug debug mode now opt-in via FLASK_DEBUG=1
f0c3da4 security: stop tracking .env.qa (real admin password) and db files
4417ad9 chore(deps): patch CVEs — flask 3.1.3, requests 2.33.0, dotenv 1.2.2, Pillow 12.2.0
c074acc fix(admin): restore lost admin.html features after bad rollback
ed3288a test: hermetic env — pytest must never hit live Stripe/Telegram/n8n
73e5bc5 fix(admin): Stripe outage can no longer block booking confirmation
8ab645d feat(WIP): balance payment button in confirmation email + June events  ← ваш WIP, сохранён как есть
```

Перед деплоем: `python3 -m pytest tests/ -q` → 326 passed, затем `flyctl deploy` только после ротации секретов.
