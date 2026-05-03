# 📋 ТЕХНИЧЕСКОЕ ЗАДАНИЕ
## Система бронирования мини-сессий Pashynska Photography

---

## 1. ОБЩАЯ ИНФОРМАЦИЯ

**Проект:** Flask-букинг для фотографа Iryna Pashynska (@pashynska.photo)  
**Текущая дата:** 3 мая 2026 (съёмка уже сегодня!)  
**Локация:** Calgary, AB, Canada  
**Репозиторий:** https://github.com/cryptoDepoth/iryna-booking  
**Путь к коду:** `~/business/iryna/booking/`  
**Сервер:** Flask на порту 5001 (`python3 app.py`)  
**База данных:** SQLite (`bookings.db`)  
**Публичный URL (туннель):** `https://boundary-pickup-connectivity-wealth.trycloudflare.com`  
**Локальный URL:** `http://localhost:5001`

---

## 2. БИЗНЕС-МОДЕЛЬ

### 2.1 Продукт
- **Название:** Blossom Mini Sessions / Lilac Mini Sessions
- **Длительность:** 20 минут
- **Цена полная:** CAD $190 + GST = $199.50
- **Депозит:** CAD $95 через Interac e-Transfer
- **e-Transfer email получателя:** `iryna.pashynska@gmail.com`
- **Вопрос для e-Transfer:** "Букинг"
- **Типы сессий:** Blossom Mini, Lilac, Maternity, Wedding

### 2.2 Слоты (3 мая 2026)
```
10:00 — ✅ Занят (Iryna)
10:30 — ✅ Занят (Anastasiia)
11:00 — ✅ Занят (Olga)
11:30 — 🟢 Свободен
12:00 — ✅ Занят (Nina)
12:30 — 🟢 Свободен
13:00 — 🟢 Свободен
13:30 — 🟢 Свободен
14:00 — 🟢 Свободен
14:30 — 🟢 Свободен
15:00 — 🟢 Свободен
15:30 — 🟢 Свободен
```

### 2.3 Флоу бронирования
1. Клиент заходит на сайт → видит календарь слотов
2. Выбивает свободный слот → жмёт "Book Slot"
3. Вводит: Name, Email, Phone, Instagram, Session Type
4. `/reserve` создаёт запись со статусом `reserved` + таймер 15 минут
5. Перенаправляет на `/payment` с инструкциями e-Transfer
6. Клиент отправляет e-Transfer на указанный email
7. `check_etransfer.py` (запускается каждые 5 мин через LaunchAgent) проверяет Gmail
8. При обнаружении e-Transfer → статус `confirmed`, депозит отмечен
9. Синхронизация с Notion
10. Email подтверждение клиенту

### 2.4 Флоу авто-отмены
- Если клиент не оплатил в течение 15 минут → слот освобождается
- Статус меняется на `expired`
- Клиент может заново забронировать

---

## 3. ТЕКУЩЕЕ СОСТОЯНИЕ СИСТЕМЫ

### 3.1 Работает ✅
- [x] Отображение слотов на календаре
- [x] Резервирование слота (`/reserve`)
- [x] Страница оплаты с таймером 15 мин (`/payment`)
- [x] Rate limiting (5 попыток / 10 мин на IP) — возвращает 429
- [x] Проверка e-Transfer через Gmail (`check_etransfer.py`)
- [x] Подтверждение бронирования (`/confirm`)
- [x] Синхронизация с Notion
- [x] Обработка данных клиента (name, email, phone, instagram)
- [x] Статистика (`/admin` — базовая)

### 3.2 Известные проблемы ⚠️
- [ ] **Race condition** — возможна одновременная бронь одного слота двумя клиентами
- [ ] **Нет endpoint /expired** — просроченные брони чистятся только при следующем запросе
- [ ] **Нет Google Calendar** — события не создаются автоматически
- [ ] **Нет SMS-напоминаний** — клиенты могут забыть о съёмке
- [ ] **Дата захардкожена** — `DATE = "2026-05-03"`, нужна поддержка нескольких дат
- [ ] **Flask dev server** — не production-ready, нужен Gunicorn

---

## 4. ТЕХНИЧЕСКИЕ ТРЕБОВАНИЯ

### 4.1 Стек
- **Backend:** Python 3.12, Flask
- **Database:** SQLite3 (`bookings.db`)
- **Frontend:** HTML templates (Jinja2), vanilla JS, Tailwind CSS
- **External APIs:**
  - Notion API (database integration)
  - Gmail IMAP (Himalaya CLI) — проверка e-Transfer
  - Cloudflare Tunnel (публичный доступ)

### 4.2 Переменные окружения (.env)
```bash
NOTION_API_KEY="YOUR_NOTION_API_KEY"
NOTION_DATABASE_ID="355510b9-cc5b-818c-aec6-d764f116e2b2"
```

### 4.3 Notion Database Schema
| Поле | Тип | Описание |
|------|-----|----------|
| Name | Title | Имя клиента |
| Date | Date | Дата съёмки |
| Time | Rich Text | Время съёмки |
| Session Type | Select | Blossom Mini / Lilac / Maternity / Wedding |
| Instagram | Rich Text | @handle |
| Deposit | Number | Сумма депозита (95) |
| Status | Select | Pending / Paid / Confirmed / Cancelled |
| Email | Email | Email клиента |

**Важно:** Поле имени в Notion называется `"Name"` (не `"Client Name"`). Обращение через `properties.Name.title[0].text.content`.

### 4.4 SQLite Schema
```sql
CREATE TABLE bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT,
    time TEXT,
    name TEXT,
    email TEXT,
    phone TEXT,
    instagram TEXT,
    session_type TEXT,
    status TEXT DEFAULT 'available',
    confirmed INTEGER DEFAULT 0,
    reserved_until TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(date, time)
);
```

### 4.5 Rate Limiting
- Максимум 5 запросов на `/reserve` за 10 минут с одного IP
- IP определяется через `X-Forwarded-For` header (для Cloudflare)
- При превышении: HTTP 429 + сообщение "Too many requests. Please wait 10 minutes."

---

## 5. ЗАДАЧИ ДЛЯ AI-АГЕНТА (по приоритету)

### 🔴 CRITICAL (сделать обязательно)

#### Задача 1: Исправить Race Condition в `/reserve`
**Описание:** Два клиента могут одновременно забронировать один слот.
**Текущий код:** `SELECT` → проверка → `INSERT` (неатомарно)
**Решение:** Использовать `BEGIN IMMEDIATE` + `UNIQUE` constraint или optimistic locking.
**Файл:** `app.py`, endpoint `/reserve`

#### Задача 2: Создать endpoint `/expired` + авто-очистка
**Описание:** Сейчас просроченные брони (`reserved_until < now`) не очищаются автоматически.
**Требование:**
- При каждом запросе к `/slots` — проверять и помечать просроченные как `expired`
- Добавить endpoint `/expired` для ручной очистки админом
- Запускать через cron/LaunchAgent раз в 5 минут
**Файл:** `app.py`

### 🟠 HIGH (улучшения)

#### Задача 3: Google Calendar Integration
**Описание:** При подтверждении (`/confirm`) — автоматически создавать событие в Google Calendar.
**Требования:**
- OAuth2 с Google Calendar API
- Создавать событие: title = "Mini Session — [Name]", time = [date] + [time] + 20min
- Добавлять ICS-файл в email подтверждение
- Хранить `calendar_event_id` в SQLite для отмены/изменения
**Файл:** `app.py` или отдельный `google_calendar.py`

#### Задача 4: Admin Dashboard
**Описание:** Страница `/admin` со списком всех броней.
**Требования:**
- Таблица: Date | Time | Name | Email | Phone | Instagram | Status | Actions
- Фильтр по статусу
- Кнопка "Cancel" для ручной отмены
- Кнопка "Confirm" для ручного подтверждения
- Статистика: total bookings, confirmed, pending, expired
**Файл:** `templates/admin.html`, маршрут в `app.py`

### 🟡 MEDIUM (фичи)

#### Задача 5: SMS Напоминания (Twilio)
**Описание:** Отправлять SMS клиенту за 24 часа и за 1 час до съёмки.
**Требования:**
- Интеграция Twilio API
- Сохранять `phone` в SQLite (уже есть)
- Сообщение: "Hi [Name]! Reminder: your mini session with Iryna Pashynska is tomorrow at [time]. Location: [address]. See you there! 🌸"
- Запускать через cron/LaunchAgent
**Файл:** новый `sms_reminders.py`

#### Задача 6: Поддержка нескольких дат
**Описание:** Сейчас `DATE = "2026-05-03"` — захардкожено.
**Требования:**
- Передать дату через URL: `/?date=2026-05-10`
- Сохранить дату в SQLite bookings
- Отображать доступные даты в календаре
- По умолчанию — ближайшая доступная дата
**Файлы:** `app.py`, `templates/index.html`

### 🟢 LOW (рефакторинг)

#### Задача 7: Production Server
**Описание:** Заменить Flask dev server на Gunicorn.
**Команды:**
```bash
pip install gunicorn
gunicorn -w 2 -b 0.0.0.0:5001 app:app
```

#### Задача 8: Email подтверждение клиенту
**Описание:** При подтверждении e-Transfer — отправлять письмо клиенту.
**Требования:**
- HTML-письмо с деталями съёмки (время, дата, тип, что взять)
- Отправка через SMTP или SendGrid
**Файл:** новый `email_confirmation.py`

---

## 6. ОГРАНИЧЕНИЯ И ПРАВИЛА

### 6.1 Что НЕЛЬЗЯ трогать
- ❌ **Дизайн сайта** — HTML/CSS уже готов и одобрен клиентом
- ❌ **Фотографии** — только реальные фото из Instagram, никаких стоковых
- ❌ **Ценообразование** — $95 депозит, $199.50 полная сумма (+GST)
- ❌ **e-Transfer email** — только `iryna.pashynska@gmail.com`
- ❌ **Структура шаблонов** — не менять layout, только добавлять новые страницы (admin)

### 6.2 Бренд и тональность
- **Фотограф:** Iryna Pashynska
- **Instagram:** @pashynska.photo
- **Тон:** Тёплый, весенний, Calgary blossoms
- **Язык клиентского интерфейса:** Английский
- **Язык админки:** Английский (или английский + русский)

### 6.3 Доступы
- **Notion API Key:** `YOUR_NOTION_API_KEY`
- **Notion DB ID:** `355510b9-cc5b-818c-aec6-d764f116e2b2`
- **Gmail:** `iryna.pashynska@gmail.com` (проверка через Himalaya CLI)
- **GitHub:** `cryptoDepoth/iryna-booking`

### 6.4 LaunchAgent (macOS)
- Файл: `~/Library/LaunchAgents/com.pashynska.etransfer-check.plist`
- Запускает: `check_etransfer.py` каждые 5 минут
- Логи: `/tmp/etransfer_check.log`

---

## 7. ПРОВЕРКА КАЧЕСТВА (QA)

После каждой задачи:
1. ✅ Запустить `python3 app.py`, проверить что сервер стартует
2. ✅ Протестировать локально: curl /slots → /reserve → /payment → /confirm
3. ✅ Проверить SQLite через CLI: `sqlite3 bookings.db "SELECT * FROM bookings;"`
4. ✅ Проверить Notion sync — данные появились в базе?
5. ✅ Запустить через Cloudflare tunnel и проверить публичный URL

---

## 8. КОНТАКТЫ

- **Owner:** Andrzej (cryptoDepoth)
- **Фотограф:** Iryna Pashynska
- **Instagram:** https://instagram.com/pashynska.photo
- **Сервисная зона:** Calgary / Airdrie / Cochrane / Okotoks / Chestermere

---

**Последнее обновление:** 3 мая 2026, 12:30 PM MST
