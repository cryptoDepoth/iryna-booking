# Manual Booking & Block Slot — Quick Spec

## Фичи

### 1. Block Slot (Показать слот как недоступный)
- **Где**: Admin → любой день в /slots
- **Что делает**: Добавляет запись в БД со статусом `blocked` — слот пропадает из публичного `/slots`
- **Поля**: date, time, reason (обед, офлайн, личное)
- **Удаление**: Нажать "Unblock" → удаляет запись

### 2. Manual Booking (Забронировать под кого-то)
- **Где**: Admin → любой день → "Manual Booking"
- **Форма**: date, time, name, email, phone, session_type, deposit
- **Что делает**: Создаёт confirmed бронирование (не нужен 15-мин таймер)
- **Email**: Идёт confirmation клиенту
- **Синхронизация**: Добавляется в clients таблицу

## Schema changes

```sql
ALTER TABLE bookings ADD COLUMN blocked INTEGER DEFAULT 0;
ALTER TABLE bookings ADD COLUMN block_reason TEXT DEFAULT NULL;
```

## Endpoints

- `POST /admin/block-slot` — заблокировать
- `POST /admin/unblock-slot` — разблокировать
- `POST /admin/manual-booking` — ручное бронирование
- `GET /admin/slots/<date>` — посмотреть заблокированные + забронированные

## get_slots fix

```sql
WHERE date=? AND status NOT IN ('cancelled','expired')
  AND (blocked=0 OR blocked IS NULL)
```

## UI (admin.html)

- Новый блок "🔒 Manage Slots" рядом с "Add Event"
- Дата + время picker
- Кнопки: "Block" (причина) / "Manual Booking" (форма) / "Unblock"
- Таблица заблокированных слотов

---

**Чтобы не потерять контекст**: нужно добавить `blocked` колонку + 3 endpoint'а + обновить get_slots + HTML. Это ~100 строк кода. Делаю в следующей сессии если хочешь, или ты можешь передать это Claude Code на компьютере.
