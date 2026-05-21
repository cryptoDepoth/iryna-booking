# API Reference — Pashynska Booking System v1

All endpoints are on the Flask server (default: `http://localhost:5001`).

---

## Public Endpoints

### `GET /`

Landing page. Shows event details and available time slots.

**Response:** HTML page (`templates/index.html`)

---

### `GET /slots/<date_str>`

Returns available time slots for a given date.

**URL Params:**
- `date_str` — Date in `YYYY-MM-DD` format

**Response:**
```json
{
  "date": "2026-05-03",
  "slots": [
    {"label": "10:00 – 10:20", "time": "10:00"},
    {"label": "10:30 – 10:50", "time": "10:30"}
  ],
  "total": 12,
  "available": 8
}
```

---

### `POST /reserve`

Reserve a time slot. Holds it for 15 minutes.

**Request Body (JSON):**
```json
{
  "time": "10:00"
}
```

**Success Response (200):**
```json
{
  "success": true,
  "booking_id": 42,
  "expires_at": "2026-05-03T14:30:00",
  "message": "Reserved for 15 minutes. Complete payment before 14:30."
}
```

**Error Responses:**
- `400` — Invalid time slot
- `409` — Slot already taken
- `429` — Rate limited (too many requests)

---

### `GET /payment`

Payment instructions page. Shows e-Transfer details and client info form.

**Query Params:**
- `booking_id` — Required. The reservation ID from `/reserve`

**Response:** HTML page (`templates/payment.html`)

---

### `POST /confirm`

Submit client details and trigger payment verification.

**Request Body (JSON):**
```json
{
  "booking_id": 42,
  "name": "Jane Doe",
  "email": "jane@example.com",
  "phone": "+14035551234",
  "instagram": "@janedoe",
  "session_type": ""
}
```

**Success Response (200):**
```json
{
  "success": true,
  "booking_id": 42,
  "checker_started": true,
  "telegram_notified": true,
  "expires_at": "2026-05-03T14:50:00",
  "message": "Booking received. Payment verification is now active for about 20 minutes."
}
```

**Error Responses:**
- `400` — Missing required fields (name, email, phone)
- `404` — Booking not found
- `409` — Booking not in correct status
- `410` — Reservation expired

---

### `GET /success`

Live status page. Auto-updates via JavaScript polling.

**Query Params:**
- `booking_id` — Required

**Response:** HTML page (`templates/success.html`)

---

### `GET /booking-status`

Lightweight JSON endpoint for client-side polling. Called every 3 seconds by success.html.

**Query Params:**
- `booking_id` — Required

**Response:**
```json
{
  "id": 42,
  "status": "pending_payment",
  "confirmed": false,
  "paid": false
}
```

When confirmed:
```json
{
  "id": 42,
  "status": "confirmed",
  "confirmed": true,
  "paid": true
}
```

**Error Responses:**
- `400` — Missing or invalid booking_id
- `404` — Booking not found

---

## Admin Endpoints (HTTP Basic Auth)

All admin endpoints require `Authorization: Basic <base64(user:password)>` header.

### `GET /admin`

Admin dashboard. Shows all bookings with stats.

**Query Params:**
- `format=json` — Return JSON instead of HTML

**Response:** HTML (`templates/admin.html`) or JSON

---

### `POST /admin/confirm`

Manually confirm a booking as paid.

**Request Body (JSON or form):**
```json
{
  "booking_id": 42
}
```

**Response:**
```json
{
  "success": true,
  "message": "Booking #42 confirmed"
}
```

---

### `POST /admin/cancel`

Cancel a booking and release the slot.

**Request Body (JSON or form):**
```json
{
  "booking_id": 42
}
```

**Response:**
```json
{
  "success": true,
  "message": "Booking #42 cancelled, slot released"
}
```

---

### `GET /admin/event`

View current event configuration.

**Response:**
```json
{
  "title": "🪻 Blossom Mini Sessions",
  "date": "2026-05-03",
  "deposit": 95,
  "..."
}
```

---

### `POST /admin/event`

Update event configuration fields.

**Request Body (JSON):**
```json
{
  "title": "New Event Title",
  "deposit": 100
}
```

**Response:**
```json
{
  "success": true,
  "config": { "..." }
}
```

---

## Webhook Endpoint

### `POST /telegram/webhook`

Receives Telegram Bot API callback queries from inline buttons.

**Request Body:** Standard Telegram `Update` object with `callback_query`

**Callback Data Formats:**
- `confirm:<booking_id>` — Confirm booking as paid
- `cancel:<booking_id>` — Cancel booking and release slot

**Response:** Empty 200 (with `answerCallbackQuery` via Bot API)

**Security:** Only processes callbacks from `TELEGRAM_CHAT_ID`.

---

## Utility Endpoints

### `GET /images/<filename>`

Serve static images from `static/images/`.

---

### `GET /expired`

Trigger cleanup of expired reservations.

**Response:**
```json
{
  "success": true,
  "released": 3,
  "message": "3 expired slot(s) released"
}
```
