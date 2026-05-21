# Claude Code Audit Instructions — Pashynska Booking System

## Контекст

Это production booking-сайт для Iryna Pashynska Photography:
- **Live URL**: https://book.pashynskaphoto.com (или https://pashynska.agency)
- **Backend**: Python/Flask на Fly.io (app: `iryna-booking`)
- **DB**: SQLite на Fly.io Volume
- **Email**: через Himalaya CLI (IMAP/SMTP на Gmail)
- **Domain**: Cloudflare обратный-прокси к iryna-booking.fly.dev

## Где что находится

```
/Users/andrzej/Iryna-Master/01-Booking-System/
├── app.py                 — основной Flask (5400+ строк)
├── templates/
│   ├── index_v2.html      — главная страница с бронированием
│   ├── admin.html         — панель администратора
│   ├── booking_detail.html — карточка клиента (новая)
│   └── payment.html       — страница оплаты
├── .bak.2026-05-13/       — бэкап до security/design изменений
├── .bak.2026-05-16/       — бэкап до booking detail changes
├── tests/                 — pytest suite
├── requirements.txt       — зависимости
├── fly.toml               — Fly.io конфиг
└── .env                   — секреты (ADMIN_PASSWORD, STRIPE_SECRET, etc.)
```

## Что было сделано недавно (2026-05-16)

### 1. Security fixes (Клод)
- Stripe webhook fail-closed
- Cookie SECURE/HTTPONLY/SAMESITE
- |e → |tojson в JS-контексте (XSS fix)
- HSTS + CSP + Permissions-Policy headers
- datetime.utcnow → timezone-aware
- bare except → except Exception
- booking-status через hmac.compare_digest
- reCAPTCHA soft-fallback (не блокирует реальных клиентов)
- Honeypot anti-bot поле

### 2. Дизайн (21st.dev)
- Aurora glow + dot grid фон
- Glass cards с backdrop-blur
- Tabular-nums для цифр
- Min 38-48px touch targets
- Shimmer на primary CTA

### 3. Booking Detail Page (НОВОЕ)
- `/admin/booking/<id>` — карточка клиента
- Generate Invoice (PDF через ReportLab)
- Send Invoice (email с PDF attachment)
- Wfolio Gallery (сохранить URL + email клиенту)
- Request Google Review (email с кнопкой)
- Reschedule (перенос бронирования)

## Что нужно проверить

### А. Live Site Smoke Test
```bash
# Открыть сайт и проверить:
# 1. Главная загружается без ошибок
# 2. reCAPTCHA не блокирует бронирование (попробуй забронировать слот)
# 3. Admin login работает
# 4. Клик на клиента открывает /admin/booking/<id>
# 5. Generate Invoice открывает PDF
# 6. Send Invoice отправляет email
# 7. Wfolio обновляет поле и отправляет email
# 8. Request Review отправляет email

# Проверить через curl:
curl -sI https://book.pashynskaphoto.com/
curl -s https://book.pashynskaphoto.com/events
curl -s https://book.pashynskaphoto.com/slots/2026-05-16
```

### Б. Security Audit
Проверить:
1. **Headers** — HSTS, CSP, X-Content-Type-Options, X-Frame-Options
2. **SQL Injection** — используются ли parameterized queries
3. **XSS** — |tojson для JS-переменных, |e для HTML
4. **CSRF** — защищены ли POST endpoints
5. **Auth** — admin_required, ADMIN_PASSWORD comparison
6. **Secrets** — нет ли plaintext паролей в .env или коде
7. **File Upload** — есть ли upload endpoints, проверяются ли типы
8. **Rate Limiting** — защита от перебора на admin и reserve
9. **Logs** — нет ли PII в stdout
10. **Backup** — `.bak` файлы без секретов

### В. Code Quality
1. Проверить deprecated функции (datetime.utcnow остался где-то?)
2. Проверить bare except
3. Проверить открытые TODO-комментарии
4. Проверить неиспользуемые imports
5. Запустить pyright/mypy если установлены

### Г. DB Integrity
```bash
# Проверить колонки в bookings:
sqlite3 ~/.pashynska-data/bookings.db ".schema bookings"

# Проверить что wfolio_url колонка существует (добавлена 2026-05-16):
sqlite3 ~/.pashynska-data/bookings.db "SELECT * FROM sqlite_master WHERE sql LIKE '%wfolio_url%'"
```

### Д. Email / Himalaya
```bash
himalaya account list
# Проверить отправку:
himalaya message list
himalaya message read <ID>
```

### Е. Fly.io
```bash
# Проверить статус:
flyctl status --app iryna-booking
flyctl logs --app iryna-booking
# Проверить secrets (только имена, не значения):
flyctl secrets list --app iryna-booking
```

## Как запускать тесты

```bash
cd /Users/andrzej/Iryna-Master/01-Booking-System
source .venv/bin/activate || python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
pytest tests/ test_admin.py -q --tb=short
# Ожидается: 115 passed, 1 skipped
```

## Чек-листы — что нашёл / что проверил

- [ ] Live site загружается (200 OK)
- [ ] reCAPTCHA не блокирует реальных клиентов
- [ ] Booking detail page работает
- [ ] Generate Invoice генерирует PDF
- [ ] Send Invoice отправляет email с attachment
- [ ] Wfolio обновляет БД и отправляет email
- [ ] Request Review отправляет email
- [ ] Security headers присутствуют
- [ ] Нет новых SQL injection уязвимостей
- [ ] Нет XSS в новых endpoint'ах
- [ ] Все тесты проходят
- [ ] Нет plaintext секретов в коде
- [ ] Бэкапы на месте (`.bak.2026-05-13/` и `.bak.2026-05-16/`)

## Команда для запуска

```bash
cd /Users/andrzej/Iryna-Master/01-Booking-System
pytest tests/ test_admin.py -q --tb=short
flyctl status --app iryna-booking
```

## Результат

Выведи конкретный отчёт:
1. Что протестировано (live + локально)
2. Сколько тестов прошло
3. Любые ошибки или предупреждения
4. Потенциальные уязвимости (даже если теоретические)
5. Рекомендации по улучшению
6. Перечень изменённых файлов (если что-то пофиксил)
