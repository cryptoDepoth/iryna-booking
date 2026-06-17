# Claude Code Tasks — Iryna Booking System

Выполняй задачи по порядку. Каждая задача — отдельный коммит.

**Правила:**
- НЕ меняй ничего за пределами описанных задач
- НЕ рефакторь соседний код
- После каждой задачи запускай тесты: `python -m pytest tests/ -o 'addopts=' -q`
- Все файлы проекта в текущей директории

---

## Task 1: Fix e-Transfer auto-confirmation timing bug

**Problem:** When Interac email arrives after reservation expires (15 min), the booking stays unconfirmed forever. Root cause: `expire_reservations()` changes status to `'expired'`, but `get_pending_bookings()` only looks for `status IN ('reserved', 'pending_payment')`. The `grace_minutes=60` parameter is useless because status filter kills it first.

**Files to change:**
- `check_etransfer_v2.py` — function `get_pending_bookings()`
- `app.py` — function `expire_reservations()` (optional: add grace period before expiring)

**Fix approach (choose the simplest):**

Option A: In `get_pending_bookings()`, expand the status filter to also include `'expired'`:
```python
WHERE status IN ('reserved', 'pending_payment', 'expired')
AND confirmed = 0
AND paid = 0
```
This way, even after `expire_reservations()` marks it expired, the watcher still tries to match payment.

Option B: In `expire_reservations()`, add a grace period — don't expire bookings that were created less than 60 minutes ago (Interac email delay window).

Go with Option A — it's simpler and the `grace_minutes` logic already handles the time window correctly.

**Also fix:** In the watcher thread in `app.py` (around line 225), also try to match `'expired'` bookings. After confirming an expired booking via payment match, restore its status to `'confirmed'` and set `reserved_until=NULL`.

**Test:** Add a test case in `tests/` that creates a booking, expires it, then confirms payment email arrives — should auto-confirm.

---

## Task 2: Make Instagram required on booking form

**Problem:** Instagram field is optional. Clients skip it, making follow-up harder.

**Files to change:**
- `templates/index_v2.html` — `validateBookingForm()` function and the waitlist form
- `app.py` — `_validate_booking_fields()` function
- `templates/index_v2.html` — i18n strings for error messages in all 4 languages (en, ru, hi, uk)

**Changes:**

### 2a. Frontend validation (`templates/index_v2.html`)

In `validateBookingForm()` (around line 1393), change the Instagram section from:
```js
// Instagram: optional, but if provided must be valid handle
if (ig) {
  const handle = ig.startsWith('@') ? ig.slice(1) : ig;
  if (!/^[A-Za-z0-9_.]{1,30}$/.test(handle)) {
    showFieldError('fIg', T('err_ig'));
    valid = false;
  }
}
```
To:
```js
// Instagram: REQUIRED
if (!ig) {
  showFieldError('fIg', T('err_ig_required'));
  valid = false;
} else {
  const handle = ig.startsWith('@') ? ig.slice(1) : ig;
  if (!/^[A-Za-z0-9_.]{1,30}$/.test(handle)) {
    showFieldError('fIg', T('err_ig'));
    valid = false;
  }
}
```

Also do the same for the waitlist form (search for `wIg` field — around line 1857).

### 2b. Backend validation (`app.py`)

In `_validate_booking_fields()` (around line 2896), change:
```python
# Instagram: optional — if provided must be @handle or handle (1-30 alphanumeric/._)
if instagram:
    handle = instagram.lstrip("@")
    if not _re.match(r"^[A-Za-z0-9_.]{1,30}$", handle):
        return False, "Instagram handle should be 1–30 characters (letters, numbers, . or _)"
```
To:
```python
# Instagram: REQUIRED
if not instagram or not instagram.strip():
    return False, "Instagram handle is required"
handle = instagram.strip().lstrip("@")
if not _re.match(r"^[A-Za-z0-9_.]{1,30}$", handle):
    return False, "Instagram handle should be 1–30 characters (letters, numbers, . or _)"
```

### 2c. i18n strings (`templates/index_v2.html`)

Add `err_ig_required` key to all 4 language objects:

**English** (around line 2037):
```js
err_ig_required: 'Instagram handle is required',
```

**Russian** (around line 2206):
```js
err_ig_required: 'Укажите ваш Instagram',
```

**Hindi** (around line 2370):
```js
err_ig_required: 'Instagram हैंडल ज़रूरी है',
```

**Ukrainian** (around line 2534):
```js
err_ig_required: "Вкажіть ваш Instagram",
```

### 2d. Placeholder text

Change placeholder from `@instagram (optional)` to `@instagram` in all languages:

- `ig_ph: '@instagram (optional)'` → `ig_ph: '@instagram *'`
- `ig_ph: '@instagram (необязательно)'` → `ig_ph: '@instagram *'`
- etc.

---

## Task 3: Add booking data editing in admin panel

**Problem:** If client enters wrong email/phone/Instagram, admin cannot fix it in the booking. Only `clients` table has an edit API, not `bookings` table.

**Files to change:**
- `app.py` — new API endpoint
- `templates/booking_detail.html` — make fields editable with save button

### 3a. New API endpoint in `app.py`

Add after the existing `admin_update_invoice` route (around line 4970):

```python
@app.route("/admin/booking/<int:booking_id>/edit", methods=["POST"])
@admin_required
def admin_edit_booking(booking_id):
    """Allow admin to edit client contact info on a booking: email, phone, instagram."""
    data = request.get_json(silent=True) or {}
    conn = db_conn()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Booking not found"}), 404

    fields = {}
    if "email" in data:
        email = str(data["email"]).strip()
        if email and _re.match(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$", email):
            fields["email"] = email
        else:
            conn.close()
            return jsonify({"error": "Invalid email format"}), 400
    if "phone" in data:
        fields["phone"] = str(data["phone"]).strip()[:30]
    if "instagram" in data:
        ig = str(data["instagram"]).strip().lstrip("@")[:80]
        fields["instagram"] = ig

    if not fields:
        conn.close()
        return jsonify({"error": "Nothing to update"}), 400

    set_clause = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE bookings SET {set_clause} WHERE id=?",
                 list(fields.values()) + [booking_id])
    conn.commit()
    conn.close()

    # Also update the clients table if this booking has a linked client
    try:
        booking = dict(row)
        old_email = booking.get("email", "")
        new_email = fields.get("email", old_email)
        # Update client record too
        conn2 = db_conn()
        client_fields = {}
        if "email" in fields:
            client_fields["email"] = fields["email"]
        if "phone" in fields:
            client_fields["phone"] = fields["phone"]
        if "instagram" in fields:
            client_fields["instagram"] = fields["instagram"]
        if client_fields:
            # Find client by email (old or new)
            for email_lookup in [old_email, new_email]:
                if email_lookup:
                    match = conn2.execute("SELECT id FROM clients WHERE email=?", (email_lookup,)).fetchone()
                    if match:
                        client_set = ", ".join(f"{k}=?" for k in client_fields)
                        conn2.execute(f"UPDATE clients SET {client_set} WHERE id=?",
                                      list(client_fields.values()) + [match["id"]])
                        break
        conn2.commit()
        conn2.close()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"[booking-edit] Client table sync failed: {e}")

    return jsonify({"success": True, "booking_id": booking_id, "updated_fields": list(fields.keys())})
```

### 3b. Make fields editable in `templates/booking_detail.html`

Around line 240-255 where client info is displayed, change the static spans to editable inputs with a save button.

Currently:
```html
<span class="val"><a href="mailto:{{ booking.email|e }}">{{ booking.email|e }}</a></span>
```

Change to:
```html
<span class="val" id="booking-email-display">
  <a href="mailto:{{ booking.email|e }}">{{ booking.email|e if booking.email else '—' }}</a>
</span>
<span class="val" id="booking-email-edit" style="display:none">
  <input type="text" id="edit-booking-email" value="{{ booking.email|e }}" 
         style="width:100%;padding:4px 8px;border:1px solid #e0d0d4;border-radius:6px;font-size:14px;">
</span>
```

Do the same for phone and instagram fields.

Add an "Edit" button and "Save" button:
```html
<button class="btn btn-outline" onclick="toggleBookingEdit()" id="edit-info-btn" style="font-size:12px;padding:4px 10px;">✏️ Edit</button>
<button class="btn btn-accent" onclick="saveBookingInfo({{ booking.id }})" id="save-info-btn" style="display:none;font-size:12px;padding:4px 10px;">💾 Save</button>
<button class="btn btn-outline" onclick="cancelBookingEdit()" id="cancel-edit-btn" style="display:none;font-size:12px;padding:4px 10px;">Cancel</button>
```

Add JS functions:
```js
function toggleBookingEdit() {
  document.querySelectorAll('[id$="-display"]').forEach(el => el.style.display = 'none');
  document.querySelectorAll('[id$="-edit"]').forEach(el => el.style.display = 'inline');
  document.getElementById('edit-info-btn').style.display = 'none';
  document.getElementById('save-info-btn').style.display = 'inline';
  document.getElementById('cancel-edit-btn').style.display = 'inline';
}
function cancelBookingEdit() {
  document.querySelectorAll('[id$="-display"]').forEach(el => el.style.display = 'inline');
  document.querySelectorAll('[id$="-edit"]').forEach(el => el.style.display = 'none');
  document.getElementById('edit-info-btn').style.display = 'inline';
  document.getElementById('save-info-btn').style.display = 'none';
  document.getElementById('cancel-edit-btn').style.display = 'none';
}
async function saveBookingInfo(bookingId) {
  const data = {};
  const email = document.getElementById('edit-booking-email').value.trim();
  const phone = document.getElementById('edit-booking-phone').value.trim();
  const ig = document.getElementById('edit-booking-instagram').value.trim();
  if (email) data.email = email;
  if (phone !== undefined) data.phone = phone;
  if (ig !== undefined) data.instagram = ig;
  
  const r = await api('POST', `/admin/booking/${bookingId}/edit`, data);
  if (r.ok && r.data.success) {
    showToast('Client info updated ✓', 'ok');
    setTimeout(() => location.reload(), 500);
  } else {
    showToast((r.data && r.data.error) || 'Failed to update', 'err');
  }
}
```

---

## Task 4: Reorganize admin panel — event-centric navigation

**Problem:** Currently admin shows a flat list of all clients/bookings mixed together. Hard to navigate when multiple events are active.

**Goal:** Restructure admin into event-centric dashboard:

### 4a. New admin landing page

Change the admin dashboard (`/admin`) to show:

1. **"Create Event"** button at the top
2. **"Active Events"** section — cards for each active event showing:
   - Event title, date, location
   - Slots: X available / Y total
   - Bookings: X confirmed / Y pending / Z total
   - Click → goes to event detail page
3. **"Past Events"** collapsible section — same cards but for past events

### 4b. Event detail page (`/admin/event/<event_id>`)

A new page showing everything for ONE event:

1. **Event info card** — title, date, location, pricing, edit button
2. **Slots grid** — visual timeline showing all time slots with status (available / reserved / confirmed / blocked)
3. **Bookings list** — table of all bookings for this event with:
   - Client name, email, phone, Instagram
   - Slot time, status, payment status
   - Quick actions: confirm, cancel, edit, send reminder
4. **Stats bar** — confirmed count, revenue, pending payments, available slots

### 4c. Files to create/modify

**New files:**
- `templates/admin_events.html` — event-centric dashboard (replaces current flat admin)
- `templates/admin_event_detail.html` — single event detail page

**Modified files:**
- `app.py` — add routes:
  - `GET /admin/event/<event_id>` — event detail page
  - Keep existing `/admin` working but redirect to event-centric view
  - Keep existing `/admin/clients` as a separate page accessible from sidebar
- `templates/admin_base.html` or layout — add sidebar navigation:
  - Events (main page)
  - Clients (existing page)
  - Backups (existing page)

**Design approach:**
- Keep the existing color scheme (rose/cream/blush palette)
- Use the same card-based layout as booking_detail.html
- Mobile-friendly (the admin uses it from phone too)

**Implementation notes:**
- Read events from `events.yaml` via existing `get_event_by_id()` / `get_active_event()` functions
- Count bookings per event from SQLite: `SELECT status, COUNT(*) FROM bookings WHERE event_id=? GROUP BY status`
- Slots come from `events.yaml` `time_slots` config
- Don't remove existing admin pages — just reorganize navigation
- The sidebar should have: Events, Clients, Settings, Backups, Logout

---

## Task 5: Handle e-Transfer amount mismatches — fuzzy matching with over/under logic

**Problem:** Clients sometimes pay a different amount than expected (e.g., deposit is $110 but client sends $120). Currently `match_by_amount_only()` does EXACT match only (`abs(amount - expected) < 0.01`). If amounts don't match exactly → payment goes to "orphan" → admin has to manually figure it out.

**Business rules (from owner):**
1. **Client paid MORE or EQUAL** → auto-confirm immediately, record actual amount. This is fine — maybe client chose 50% deposit instead of minimum and paid full session price upfront. Either way, confirm and calculate remaining balance as `full_price - actual_paid`.
2. **Client paid LESS** (significantly underpaid) → DO NOT auto-confirm. Record the payment amount in DB, but keep booking unconfirmed. Send Telegram notification to admin: "Client paid $X instead of expected $Y. Booking NOT confirmed. Action needed."
3. **Exact match** → existing behavior, auto-confirm.

**Files to change:**
- `check_etransfer_v2.py` — `match_by_amount_only()`, new fuzzy logic, new underpayment handling, admin notifications
- `app.py` — new endpoint or status for partial payment, ensure `_booking_paid_amount()` uses actual amount

### 5a. Fuzzy amount matching in `check_etransfer_v2.py`

Replace `match_by_amount_only()` (around line 225) with fuzzy matching:

```python
def match_by_amount_only(amount, bookings):
    """Match payment to booking(s) by amount.
    
    Strategy:
    1. Exact match (within $0.01) — high confidence, auto-confirm
    2. Overpayment (paid > expected, within 2x) — medium confidence, auto-confirm
    3. Underpayment (paid < expected but > 0) — record but DO NOT confirm
    4. No reasonable match — orphan
    
    Returns (booking, ambiguity_list, match_type) where:
      match_type = 'exact' | 'overpaid' | 'underpaid' | None
    """
    exact = []
    overpaid = []
    underpaid = []
    
    for b in bookings:
        expected = get_expected_amount_for_booking(b["id"])
        diff = amount - expected
        
        if abs(diff) < 0.01:
            exact.append(b)
        elif diff > 0 and amount <= expected * 2:
            # Paid more (up to 2x expected) — e.g., paid full price instead of deposit
            overpaid.append((b, diff))
        elif diff < 0 and amount >= expected * 0.3:
            # Paid less but at least 30% of expected — still a legitimate partial payment
            underpaid.append((b, abs(diff)))
    
    # Priority 1: exact match
    if exact:
        if len(exact) == 1:
            return exact[0], [], 'exact'
        return None, exact, None
    
    # Priority 2: overpayment — auto-confirm
    if overpaid:
        overpaid.sort(key=lambda x: x[1])  # closest overpayment first
        return overpaid[0][0], [], 'overpaid'
    
    # Priority 3: underpayment — do NOT auto-confirm, just record
    if underpaid:
        underpaid.sort(key=lambda x: x[1])  # closest underpayment first
        return underpaid[0][0], [], 'underpaid'
    
    return None, [], None
```

### 5b. New DB function: record partial payment WITHOUT confirming

Add to `check_etransfer_v2.py`:

```python
def record_partial_payment(booking_id, paid_amount):
    """Record a partial/underpayment without confirming the booking.
    
    Updates paid_amount on the booking but keeps confirmed=0, paid=0.
    Sets status to 'partial_payment' so admin can see it needs attention.
    """
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        UPDATE bookings
        SET paid_amount=?, status='partial_payment'
        WHERE id=?
    """, (paid_amount, booking_id))
    updated = c.rowcount
    conn.commit()
    conn.close()
    return updated > 0
```

### 5c. Update `check_single_email()` to handle all match types

In `check_single_email()` (around line 365), change the matching section:

```python
    matched, ambiguous, match_type = match_by_amount_only(amount, bookings)
    
    if ambiguous:
        # ... existing ambiguity handling stays the same
        pass

    if matched is None:
        # ... existing orphan handling stays the same
        pass

    expected = get_expected_amount_for_booking(matched["id"])
    
    if match_type in ('exact', 'overpaid'):
        # Auto-confirm with ACTUAL amount from email
        if confirm_booking(matched["id"], amount):
            mark_message_processed(msg_id, matched["id"], amount)
            
            if match_type == 'overpaid':
                _notify_admin_overpaid(matched, expected, amount)
            
            print(f"   ✅ CONFIRMED Booking #{matched['id']} — ${amount:.2f} ({match_type})")
            return matched["id"], None
    
    elif match_type == 'underpaid':
        # DO NOT confirm — just record payment and alert admin
        if record_partial_payment(matched["id"], amount):
            mark_message_processed(msg_id, matched["id"], amount)
            _notify_admin_underpaid(matched, expected, amount)
            print(f"   ⚠️ UNDERPAID Booking #{matched['id']} — ${amount:.2f} (expected ${expected:.2f}), NOT confirmed")
            return matched["id"], None
        else:
            print(f"   ❌ Failed to record partial payment for #{matched['id']}")
            return None, None
```

### 5d. Admin notifications

Add two new notification functions:

```python
def _notify_admin_overpaid(booking, expected, actual):
    """Notify admin: client paid more than expected, auto-confirmed."""
    try:
        from app import _tg_message
        diff = actual - expected
        total = _get_booking_full_price(booking)
        balance = max(total - actual, 0)
        lines = [
            f"💰 **Payment received (overpaid)**",
            f"",
            f"Booking #{booking['id']} — {booking.get('name', '?')}",
            f"Expected deposit: ${expected:.2f}",
            f"Received: ${actual:.2f}",
            f"Extra: +${diff:.2f}",
            f"",
            f"✅ Auto-confirmed",
            f"Remaining balance: ${balance:.2f}",
        ]
        _tg_message("\n".join(lines))
    except Exception as e:
        print(f"[admin] Failed to send overpaid alert: {e}")


def _notify_admin_underpaid(booking, expected, actual):
    """Notify admin: client paid less than expected, NOT confirmed."""
    try:
        from app import _tg_message
        diff = expected - actual
        lines = [
            f"⚠️ **Payment received (UNDERPAID)**",
            f"",
            f"Booking #{booking['id']} — {booking.get('name', '?')}",
            f"Expected deposit: ${expected:.2f}",
            f"Received: ${actual:.2f}",
            f"Short: -${diff:.2f}",
            f"",
            f"❌ Client NOT confirmed",
            f"Payment recorded. Action needed — confirm manually or contact client.",
        ]
        _tg_message("\n".join(lines))
    except Exception as e:
        print(f"[admin] Failed to send underpaid alert: {e}")


def _get_booking_full_price(booking):
    """Get full session price for a booking (for balance calculation)."""
    try:
        import yaml
        event_id = booking.get("event_id")
        if event_id and os.path.exists(EVENTS_YAML_PATH):
            with open(EVENTS_YAML_PATH) as f:
                data = yaml.safe_load(f)
            for ev in data.get("events", []):
                if ev.get("id") == event_id:
                    return float(ev.get("full_price", 190))
    except Exception:
        pass
    return 190.0  # default
```

### 5e. Add 'partial_payment' to admin panel visibility

In `app.py`, make sure bookings with `status='partial_payment'` show up in admin panel. Search for any status filters that might exclude it and add `'partial_payment'` where needed (likely in admin booking list queries and dashboard stats).

Also in `booking_detail.html`, show a distinct visual indicator for partial payment status — e.g., orange/yellow banner saying "Partial payment received ($X of $Y expected)".

### 5f. Verify balance calculation uses actual paid amount

`_booking_paid_amount()` in `app.py` (line 3621) already reads `paid_amount` from the bookings table. Since both `confirm_booking()` and `record_partial_payment()` write the actual email amount there, the balance calculation `full_price - paid_amount` will be correct.

**No changes needed in `app.py` for balance calculation** — it already works correctly.

### 5g. Also update `get_pending_bookings()` to include 'expired' and 'partial_payment' status

This is covered by Task 1, but the status filter should include:
```sql
WHERE status IN ('reserved', 'pending_payment', 'expired', 'partial_payment')
```

**Test:** Add test cases:
1. Client pays exact amount → auto-confirm ✅
2. Client pays MORE ($190 full price instead of $110 deposit) → auto-confirm, balance = $0, admin notified about overpayment ✅
3. Client pays LESS ($60 instead of $110) → NOT confirmed, payment recorded, admin gets Telegram alert ⚠️
4. Client pays wildly different amount ($500 instead of $110) → no match, orphan alert ❌
5. After admin manually confirms an underpaid booking → balance correct ($full_price - $actual_paid)

---

## Execution Order

1. **Task 1** (e-Transfer bug fix) — highest priority, affects revenue
2. **Task 5** (Amount fuzzy matching) — also revenue-critical, works with Task 1
3. **Task 2** (Instagram required) — simple, quick
4. **Task 3** (Booking edit in admin) — medium complexity
5. **Task 4** (Admin reorganization) — largest task, do last

After each task: run tests, verify no regressions.
