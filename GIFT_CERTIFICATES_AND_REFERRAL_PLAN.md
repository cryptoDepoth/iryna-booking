# Gift Certificates + Referral Program — Plan
# Pashynska Photography | 2026-06-16

---

## Анализ конкурентов

### Как это делают другие фотографы

**Lilyfire Photography (Calgary)** — нет публичных gift certificates на сайте. Только прямые бронирования.

**Meagan Paige (Calgary)** — нет gift certificates. Всё через Instagram DM.

**Joanna Jensen (Calgary)** — нет автоматизированной системы.

**Что делают крупные фотографы мира (HoneyBook, ShootProof, Studio Ninja):**
- Генерируют PDF-сертификат с уникальным кодом
- Код применяется при бронировании как купон
- Получатель сам выбирает дату/тип сессии
- Срок действия 6–12 месяцев

**Вывод:** ни один локальный Calgary фотограф не предлагает gift certificates онлайн. Это прямое конкурентное преимущество — особенно в декабре и на День Матери.

---

## ЧАСТЬ 1 — GIFT CERTIFICATES (Подарочные сертификаты)

### Концепция

Покупатель приходит на `/gift` → выбирает тип сессии или сумму → оплачивает → получает красивый PDF на email → дарит получателю → получатель бронирует сессию с кодом сертификата.

### UX Flow

```
/gift (landing)
  ↓ выбор пакета или суммы
  ↓ ввод: кому (имя получателя), от кого, личное сообщение
  ↓ Stripe Checkout (полная оплата, не депозит)
  ↓ Webhook → генерация кода + PDF → email покупателю + email получателю
  ↓ Получатель идёт на /book → вводит код → цена обнуляется / снижается
```

### Пакеты для сертификатов

| Пакет | Цена | Описание |
|-------|------|----------|
| Mini Session | $230 + GST = $241.50 | 30 мин, 20 фото, видео |
| Family Session | $290 + GST = $304.50 | 1 час, 30 фото |
| Maternity Session | $290 + GST = $304.50 | 1 час, 30 фото |
| Individual Session | $290 + GST = $304.50 | Private photoshoot |
| Custom Amount | $50–$500 | Сколько угодно |

### Дизайн PDF-сертификата

Красивый вертикальный PDF (A5 или квадрат):
- Фото Ирины или красивый фон из работ
- "This gift certificate entitles [Recipient Name] to a [Session Type] with Pashynska Photography"
- Код крупным шрифтом: `GIFT-XXXX-XXXX`
- Срок действия: 12 месяцев
- Подпись Ирины
- Логотип + book.pashynskaphoto.com

### БД схема (новые таблицы)

```sql
-- Сертификаты
CREATE TABLE gift_certificates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,          -- 'GIFT-A3K9-7BXM'
    purchaser_email TEXT NOT NULL,
    purchaser_name TEXT NOT NULL,
    recipient_name TEXT,
    recipient_email TEXT,
    personal_message TEXT,
    session_type TEXT,                  -- 'mini' / 'family' / 'custom'
    amount REAL NOT NULL,               -- оплаченная сумма (без GST)
    amount_with_gst REAL NOT NULL,
    stripe_payment_intent TEXT,
    status TEXT DEFAULT 'active',       -- active / redeemed / expired / refunded
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT,                    -- +12 месяцев от created_at
    redeemed_at TEXT,
    redeemed_booking_id INTEGER,
    pdf_sent BOOLEAN DEFAULT 0
);
```

### Применение при бронировании

В форме бронирования — поле "Gift Certificate Code (optional)".
При вводе кода:
1. Проверяем code в `gift_certificates` → статус `active`, не истёк
2. Если `session_type` совпадает (или `custom`) — применяем скидку
3. При подтверждении — статус `redeemed`, `redeemed_booking_id` = id бронирования

### API endpoints

```
GET  /gift                     — лендинг страница
POST /gift/checkout            — создать Stripe Checkout Session
GET  /gift/success?session_id= — после оплаты → генерация PDF + отправка email
POST /gift/validate            — AJAX: проверить код (JSON)
GET  /gift/certificate/<code>  — скачать PDF (по ссылке из email)
```

### Маркетинг Gift Certificates

**Когда продавать:**
- Декабрь: "Perfect Christmas gift for the whole family"
- Май (перед Mother's Day): "Give mom a session she'll never forget"
- Февраль (Valentine's): "Couple session for Valentine's Day"
- День рождения / годовщина

**Где размещать:**
- Кнопка в шапке booking сайта: "🎁 Buy Gift Certificate"
- Страница /gift в navigation
- Instagram Story с CTA "Give the gift of memories"
- GBP (добавить как услугу)

---

## ЧАСТЬ 2 — REFERRAL PROMO CODE SYSTEM (Реферальная программа)

### Концепция

Двусторонний referral: клиент A даёт свой код другу → друг получает $20 скидку → когда друг оплачивает депозит → код клиента A активируется → A получает $20 на следующую сессию.

**Ключевой момент:** код A активируется ТОЛЬКО когда B совершил реальную оплату (не просто забронировал). Это предотвращает злоупотребления.

### UX Flow

```
После успешного бронирования (success page / email):
  "Share your referral code and get $20 off your next session!"
  Код: REF-IRINA-A3K9

Друг приходит на /book → вводит код → видит "$20 off applied"
  ↓ Оплачивает депозит
  ↓ Система: помечает код как "triggered", отправляет A email:
    "Your friend just booked! Your $20 credit is now active for your next session."

Клиент A при следующем бронировании вводит свой реферальный код →
  автоматически применяется $20 скидка (если earned = true)
```

### БД схема

```sql
-- Реферальные коды
CREATE TABLE referral_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,              -- 'REF-PASHYN-A3K9'
    owner_email TEXT NOT NULL,             -- кому принадлежит код
    owner_name TEXT NOT NULL,
    owner_booking_id INTEGER,              -- бронирование которое породило код
    discount_for_friend REAL DEFAULT 20.0, -- скидка для нового клиента (CAD)
    reward_for_owner REAL DEFAULT 20.0,    -- награда владельцу кода
    uses_count INTEGER DEFAULT 0,
    max_uses INTEGER DEFAULT 10,           -- защита от спама
    status TEXT DEFAULT 'active',          -- active / paused / expired
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT                        -- опционально: +12 месяцев
);

-- Использования кода
CREATE TABLE referral_uses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referral_code_id INTEGER NOT NULL,
    referee_email TEXT NOT NULL,           -- кто использовал
    referee_name TEXT,
    referee_booking_id INTEGER,
    discount_applied REAL,                 -- сколько скинули другу
    payment_confirmed BOOLEAN DEFAULT 0,   -- оплатил ли депозит
    reward_triggered BOOLEAN DEFAULT 0,    -- активирован ли бонус владельцу
    reward_booking_id INTEGER,             -- на каком бронировании использовал владелец
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    confirmed_at TEXT,                     -- когда оплатил депозит
    FOREIGN KEY (referral_code_id) REFERENCES referral_codes(id)
);
```

### Логика активации

```python
# При подтверждении оплаты депозита (в notify_payment_confirmed):
def _process_referral_on_payment(booking_id):
    booking = get_booking(booking_id)
    use = db.get_referral_use_by_booking(booking_id)
    if not use:
        return
    
    # 1. Отмечаем что друг заплатил
    db.mark_referral_payment_confirmed(use.id)
    
    # 2. Активируем бонус владельцу
    db.mark_referral_reward_triggered(use.id)
    
    # 3. Email владельцу
    send_referral_reward_email(
        to=use.owner_email,
        owner_name=use.owner_name,
        friend_name=booking.name,
        reward=use.reward_for_owner,  # $20
        code=use.referral_code
    )
```

### Email для владельца кода (когда друг оплатил)

```
Subject: 🎉 Your friend [NAME] just booked! Your $20 credit is ready.

Hi [OWNER_NAME],

Great news — [FRIEND_NAME] just booked a session using your referral code.

Your $20 credit is now active! Use it on your next session at book.pashynskaphoto.com 
— just enter your referral code [CODE] at checkout.

Thank you for sharing 💛
— Iryna
```

### API endpoints

```
GET  /referral/<code>          — страница: "You were referred by X, get $20 off"
POST /referral/validate        — AJAX: проверить код (JSON response)
GET  /my-referral              — личная страница владельца (по email + token)
```

### Защита от злоупотреблений

- Один email = один раз использует чужой код
- Нельзя использовать собственный код (code owner = referee → reject)
- max_uses = 10 на код (защита от публичного распространения)
- Reward активируется только при подтверждении оплаты, не при бронировании
- Коды не работают на gift certificates (только на обычные бронирования)

---

## ЧАСТЬ 3 — EMAIL КАМПАНИЯ ДЛЯ ЗАПУСКА

### Письмо 1: Существующим клиентам (198 email в CRM)

```
Subject: A little gift from Iryna 🎁

Hi [NAME],

Thank you for trusting me with your family memories 💛

I have two things for you:

1. Your personal referral code: [CODE]
   Share it with a friend who's been thinking about booking a session.
   They get $20 off — and when they book, you get $20 off your next session too.

2. Gift certificates are now available!
   Looking for a birthday or holiday gift idea? A photo session makes 
   a beautiful, meaningful present.
   → book.pashynskaphoto.com/gift

Thank you for being part of the Pashynska Photography family.
— Iryna
```

### Письмо 2: Горячим Instagram лидам (82 HOT из CRM)

```
Subject: 20$ off your first session with Pashynska Photography

Hi [NAME],

I noticed you were interested in a session a little while back 🌸

Here's a limited offer: use code [PROMO20] at checkout and get $20 off any 
mini or family session in July 2026.

Only 5 spots left for Canoe Mini Session on July 4:
→ book.pashynskaphoto.com/?event=canoe-mini-session-2026-07-04&promo=PROMO20

— Iryna
```

---

## ЧАСТЬ 4 — ЧТО ДОБАВИТЬ В BOOKING SYSTEM

### На странице success.html (после бронирования)
- Блок: "Share your referral code" → копировать ссылку / поделиться
- Показывать уникальный код клиента

### На странице index_v2.html (главная booking)
- Поле "Promo / Gift Certificate Code" в форме
- AJAX проверка при вводе → показывает "$20 applied ✓" или "Gift certificate: Mini Session ✓"

### Новая страница /gift
- Красивый лендинг с выбором пакета
- Поле "To:", "From:", "Personal message"
- Stripe Checkout → PDF

### Навигация
- В header booking сайта добавить: "🎁 Gift Certificate"

---

## ПЛАН РЕАЛИЗАЦИИ

### Фаза 1 (2–3 дня, Hermes/Claude Code)
1. DB миграция: таблицы `gift_certificates`, `referral_codes`, `referral_uses`
2. Gift Certificate: `/gift` страница + Stripe checkout + PDF генерация (reportlab)
3. Валидация кода в форме бронирования (AJAX)
4. Базовые email шаблоны

### Фаза 2 (1–2 дня)
5. Referral: генерация кода при подтверждении бронирования
6. Логика активации бонуса при оплате друга
7. Email уведомление владельцу кода
8. Страница /referral/<code>

### Фаза 3 (маркетинг)
9. Email кампания по 198 существующим клиентам
10. DM кампания по 82 горячим лидам с промо-кодом
11. Instagram Story анонс gift certificates
12. Добавить gift certificates в GBP как услугу

---

## ОЖИДАЕМЫЙ ЭФФЕКТ

| Метрика | Прогноз |
|---------|---------|
| Gift certificates (декабрь) | 15–30 продаж × $250 avg = $3,750–$7,500 |
| Referral программа (первые 3 мес) | 50 рефералов × 30% конверсия = 15 новых клиентов × $230 = $3,450 |
| Email кампания по 198 клиентам | 5–10% конверсия = 10–20 повторных бронирований |
| DM кампания по 82 горячим лидам | 10–15% конверсия = 8–12 новых бронирований |

**Итого потенциал первые 6 месяцев: CA$8,000–$15,000 дополнительной выручки**
