# Анализ: Telegram уведомление при ручном подтверждении в админке

## Проблема
При ручном подтверждении бронирования через админку (`POST /admin/confirm`) Telegram-уведомление об успешном подтверждении НЕ отправляется. Stripe-оплата и авто-подтверждение e-Transfer отправляют — ручное подтверждение нет.

## Где живет код

### 1. Ручное подтверждение — `admin_confirm()` (app.py, строки 2874–2918)
Что делает:
- Обновляет `bookings` → `confirmed=1, paid=1, status='confirmed', paid_amount=?`
- Вызывает `sync_client()`
- Вызывает `create_calendar_event_for_booking()`
- Вызывает `sync_to_notion()`
- Вызывает `_send_client_email()`
- **НЕ вызывает Telegram уведомление**

### 2. Stripe webhook — `stripe_webhook()` (app.py, строки 2428–2561)
Что делает после `checkout.session.completed`:
- Обновляет `bookings` → `confirmed=1, paid=1, status='confirmed', paid_amount=?`
- Вызывает `create_calendar_event_for_booking()`
- Вызывает `sync_to_notion()`
- Вызывает `_send_client_email()`
- **Вызывает `_notify_admin(msg)`** со строкой `💳 <b>Stripe Payment Confirmed!</b>`

### 3. Авто-подтверждение e-Transfer — `_after_auto_payment_confirmed()` (app.py, строки 887–917)
Что делает:
- Вызывает `create_calendar_event_for_booking()`
- Вызывает `sync_to_notion()`
- Вызывает `_send_client_email()`
- **Вызывает `notify_payment_confirmed(booking_id, paid_amount)`** → `_notify_admin(msg)` со строкой `✅ <b>Auto-Confirmed: Payment Received!</b>`

## Сравнение side effects

| Этап                     | Ручное (admin/confirm) | Stripe webhook | E-transfer auto-confirm |
|--------------------------|------------------------|----------------|-------------------------|
| Обновить booking         | ✅                     | ✅             | ✅                      |
| sync_client()            | ✅                     | ❌             | ❌                      |
| create_calendar_event    | ✅                     | ✅             | ✅                      |
| sync_to_notion           | ✅                     | ✅             | ✅                      |
| _send_client_email       | ✅                     | ✅             | ✅                      |
| Telegram _notify_admin   | ❌ **ПРОБЛЕМА**        | ✅             | ✅                      |

## Причина
В `admin_confirm()` просто забыли добавить строку `_notify_admin(...)` после отправки email клиенту. Это не архитектурный дефект — это дырка в единообразии.

## Минимальный фикс
Добавить в конец `admin_confirm()` (после `_send_client_email()`) блок аналогичный Stripe:

```python
# Notify admin on Telegram
msg = (
    f"✅ <b>Booking Confirmed Manually</b>\n\n"
    f"👤 {booking.get('name', '?')}\n"
    f"📧 {booking.get('email', '?')}\n"
    f"📅 {booking.get('date', '?')} @ {booking.get('time', '?')}\n"
    f"💰 <b>${paid_amount:.2f} CAD</b>\n"
    f"🆔 Booking #{booking_id}\n\n"
    f"✅ Confirmed via admin panel · email sent to client"
)
try:
    _notify_admin(msg)
except Exception as e:
    log.error(f"[admin] Telegram notify error for #{booking_id}: {e}")
```

## Что НЕ трогаем
- Не меняем логику Stripe webhook.
- Не меняем `_after_auto_payment_confirmed()`.
- Не меняем структуру базы данных.
- Не меняем client email.
- Не меняем Notion sync.
- Не меняем calendar event.
- Не трогаем `_notify_new_reservation()` или `_notify_payment_pending()`.

## Регрессионный тест
Тест `test_admin_confirm_sends_telegram_confirmation_notification` уже добавлен в `tests/test_booking_flow.py`. Он проверяет, что после ручного подтверждения `_notify_admin` вызывается с нужными данными (имя, email, сумма, booking_id).

## Следующий шаг
1. Прогнать тест (RED).
2. Внести правку в `admin_confirm()`.
3. Прогнать все тесты (GREEN).
4. Задеплоить на Fly.
