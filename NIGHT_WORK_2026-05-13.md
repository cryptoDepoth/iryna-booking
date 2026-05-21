# Ночная работа — 2026-05-13

## Что сделано

### 1. Новая фича: кнопка «Reschedule» в админке
- **Backend:** `POST /admin/reschedule` (`app.py`)
  - Атомарный перенос через `BEGIN IMMEDIATE` (как `/reserve`)
  - Между любыми сессиями (Blossom Mini ⇄ Lilac ⇄ Maternity)
  - Конфликт на новом слоте → 409, бронь не трогается
  - Сохраняет статус: `confirmed` остаётся `confirmed`, `reserved`/`pending_payment` получают свежий 15-мин таймер
  - Депозит/`paid_amount` сохраняется без изменений
  - Email клиенту (HTML + plain), Telegram Ирине+Андрею, Notion re-sync — всё best-effort, не блокирует на ошибках
- **UI:** `templates/admin.html`
  - Кнопка «📅 Reschedule» в actions-блоке (для reserved/pending_payment и confirmed)
  - Модалка: select event → date picker → time-slot grid (грузится из `/slots/<date>?event_id=`)
  - Текущий слот клиента помечен `•` и недоступен для клика
  - Confirm со страховкой confirm() диалогом
- **Тесты:** `tests/test_reschedule.py` — 10 кейсов: auth, валидация, конфликт, no-op, сохранение статуса, ресет таймера, cross-event. **9 passed, 1 skipped** (для cross-event нужно ≥2 active events).

### 2. Security правки (P1+P2 из аудита 2026-05-13)

| ID | Что | Где |
|---|---|---|
| P1.2 | Stripe webhook fail-closed (503 без `STRIPE_WEBHOOK_SECRET`) | `app.py` /stripe/webhook |
| P1.3 | `SESSION_COOKIE_SECURE/HTTPONLY/SAMESITE=Lax` + 8h lifetime | `app.py` после `app.secret_key` |
| P1.4 | `\| e` → `\| tojson` в `onclick` (admin.html × 5 кнопок, payment.html `copyBankMsg`) | XSS в JS-контексте через имя клиента закрыт |
| P2.1 | HSTS + CSP + Permissions-Policy + frame-ancestors (для Wfolio iframe) | `app.py` `add_security_headers` |
| P2.2 | `datetime.utcnow()` → `datetime.now(timezone.utc)` | `app.py` × 2 |
| P2.3 | `except:` → `except Exception:` | check_etransfer.py × 2, check_etransfer_v2.py × 1, timed_cron.py × 2 |
| P2.4 | `booking-status` сравнение токена через `hmac.compare_digest` | `app.py:3884` |
| P0 | Plaintext admin-пароль удалён из `V2_DESIGN_SUMMARY.md` | заменён на «см. .env» |

### 3. Дизайн-апгрейд по 21st.dev паттернам (Этапы 1–5)
Дополнительный CSS-блок добавлен в **конец** `<style>` в `templates/index_v2.html` — никаких переопределений старых правил, только дополнения. Если что-то не нравится — удаляешь блок целиком, всё возвращается как было.

- Этап 1: `prefers-reduced-motion`, `tabular-nums` для цен/времени/countdown, sharp `focus-visible` rings, `viewport-fit=cover`, `theme-color`
- Этап 2: min touch targets 32/38/44/48px для lang/chips/slots/cta
- Этап 3: aurora glow (3× radial-gradient) + dot grid с маской — без WebGL
- Этап 4: glass cards (nav/hero/event-card/step) с `-webkit-backdrop-filter` fallback для Safari < 15
- Этап 5: shimmer на primary CTA только при `hover:hover and pointer:fine` (не дёргается на mobile)

### 4. Все проверки

- **68 тестов passed**, 1 skipped (нужен второй event), 0 failed — baseline +9 тестов на reschedule
- Все 12 публичных/admin endpoint smoke-test зелёные
- Все 6 security headers подтверждены в response

---

## Деплой — запусти у себя в терминале

Я не могу задеплоить из sandbox: GitHub release CDN заблокирован для `curl https://fly.io/install.sh` в этой изолированной среде. Поэтому 4 команды, которые ты запустишь у себя:

```bash
cd /Users/andrzej/Iryna-Master/01-Booking-System

# 1. Прогоняем тесты ещё раз локально — должно быть 68 passed
python3 -m pytest tests/ -q

# 2. Деплой через remote-builder (без локального docker)
flyctl deploy --remote-only

# 3. После деплоя — быстрая проверка production
curl -sI https://pashynska.agency/ | grep -iE "strict-transport|content-security|permissions-policy"
curl -s https://pashynska.agency/events | python3 -c "import json,sys; print('events:', len(json.load(sys.stdin)['events']))"
curl -sI https://pashynska.agency/stripe/webhook -X POST  # должно быть 503 (fail-closed)

# 4. Login в admin → проверить новую кнопку Reschedule
open https://pashynska.agency/admin
```

### Если деплой упадёт

- Откатить можно через `flyctl releases` → `flyctl deploy --image <previous-image-tag>`
- Или восстановить файлы из бэкапа: `01-Booking-System/.bak.2026-05-13/`

### После успешного деплоя — обязательно ротировать

1. **`ADMIN_PASSWORD`** в `.env` (был в plaintext в `V2_DESIGN_SUMMARY.md`):
   ```bash
   NEW_PWD=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
   echo "Новый пароль: $NEW_PWD"
   flyctl secrets set ADMIN_PASSWORD="$NEW_PWD" -a iryna-booking
   ```
2. Удалить `FLY_API_TOKEN.txt` после успешного деплоя — он сейчас в `.gitignore`, но любой с временным доступом к ноуту видит prod-токен. Перенести в keychain или в `~/.config/fly/`.

---

## Состояние файлов

| Файл | Строк | Назначение |
|---|---|---|
| `app.py` | 4350 (+295 от 4055) | + reschedule endpoint, + helpers, + security headers, + cookie config |
| `templates/index_v2.html` | 2406 (+130) | + 21st CSS блок |
| `templates/admin.html` | 2451 (+367) | + reschedule modal, CSS, JS, кнопка |
| `templates/payment.html` | 833 (+6) | + tojson для bank msg |
| `tests/test_reschedule.py` | 258 | новый файл, 10 кейсов |
| `V2_DESIGN_SUMMARY.md` | 119 | пароль удалён |
| `check_etransfer.py` | 430 | `except:` → `except Exception:` × 2 |
| `check_etransfer_v2.py` | 419 | × 1 |
| `timed_cron.py` | 519 | × 2 |

**Бэкапы:** `01-Booking-System/.bak.2026-05-13/` — оригиналы перед всеми правками.

---

**Status:** локально готово к деплою, все тесты зелёные. Жду тебя с командой `flyctl deploy --remote-only`.
