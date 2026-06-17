# Codex Handoff — Admin Redesign (Pashynska Booking System)

> **Для Andrzej:** передай Codex одну строку:
> *"Прочитай `CODEX-HANDOFF-ADMIN-REDESIGN.md` в корне 01-Booking-System и продолжи внедрение редизайна админки по плану. Дизайн одобрен. Не ломай существующий /admin и 59 тестов — строй параллельным маршрутом /admin/v2."*

**Status:** Design APPROVED 2026-06-14. **P1–P3 IMPLEMENTED** (dashboard, session page + photo uploader, clients CRM) — all live behind `?v=2`, legacy untouched.

### What is DONE (live, opt-in via `?v=2`)
- `/admin?v=2` → new dashboard (`admin_pro.html`), real data.
- `/admin/event/<id>?v=2` → new session page (`admin_event_pro.html`) with **working drag&drop photo uploader** wired to `upload-batch`/`upload`/`delete`, slot board + roster from the slots API.
- `/admin/clients?v=2` → new CRM (`admin_clients_pro.html`), search/tag/sort + **JS pagination** (25/page) over the existing client APIs, detail panel with notes + tag toggles.
- 3 guarded switches in `app.py` (lines ~6756, ~9003, ~9023): default (no `?v=2`) renders the **legacy** templates unchanged → 59 tests unaffected.
- Styled to match the **public booking page** (rose/nude/ink palette + Cormorant/Tenor Sans/Inter), tokens from `index_v2.html`.
- Verified: full suite **405 passed, 1 skipped**; all 3 v2 pages return 200 + load `admin-pro.css`; legacy pages unchanged; every button endpoint (photo upload/replace/delete, slots, clients, note, tag) responds correctly; admin auth enforced (302 without key).

### What still routes to CLASSIC (intentional, low-risk)
- Slot mutations (block/unblock/manual-book) and the event **edit form** → buttons link to the classic page. Port later if wanted (APIs already exist: `block-slot`, `unblock-slot`, `manual-book`).
- Final swap (P5): point `/admin` default at the new templates + update tests that assert legacy markup.

**Author:** Claude (Cowork).

---

## 1. Goal

Make the admin **professional, scannable, and scalable** as events & clients grow. Three surfaces, one consistent design language:

1. **Dashboard** — events overview + bookings table (search, filter chips, pagination).
2. **Session detail** — slot board + client roster + **redesigned photo uploader** (drag & drop). This is the #1 pain: "добавить фото" is currently awkward.
3. **Clients CRM** — searchable, **paginated** two-pane list (1,713 clients today) + detail panel.

Improve: **appearance**, **findability/structure**, **speed of actions**. Keep the existing **orange brand** (`--accent:#f97316`) — this is an evolution, not a foreign theme.

---

## 2. Artifacts already produced (in repo root / static / templates)

| File | What it is | Use |
|---|---|---|
| `admin-redesign-preview.html` | **Clickable mock**, the approved target. Standalone, sample data. | Visual source of truth. Open in a browser, copy markup/interactions. |
| `static/css/admin-pro.css` | **Production design system** extracted from the mock (tokens, sidebar, cards, tables, pills, dropzone, CRM two-pane). | `<link>` it from the NEW templates only. |
| `templates/admin_pro.html` | New **dashboard** template, wired to real Jinja vars (may be partial — finish per §7). | Rendered by the parallel route `/admin/v2`. |

---

## 3. HARD SAFETY RULES (do not violate)

- **Do NOT modify the existing `/admin` route or `templates/admin.html`** until the new UI is fully QA'd and approved to swap. Tests assert on current markup (see §6).
- **Do NOT touch the database, `clients` table, or the 1,713 real clients.** Read-only against real data.
- **Preserve every existing route, JSON shape, and JS endpoint.** The new UI must call the *same* backend (see §5, §8).
- **`python3 -m pytest tests/ -q` must stay green (59 passed).** Run before and after every change.
- **Build behind a parallel route `/admin/v2`** (additive). Only after sign-off: make `/admin` render the new template and keep the old one as `admin_legacy.html`.
- `.env` secrets, Stripe keys, `bookings.db` — never commit, never log.

---

## 4. Rollout plan (incremental, reversible)

1. **P1 — Shell:** add route `/admin/v2` → `admin_pro.html` (dashboard) + `admin-pro.css`. No behaviour change to `/admin`. *(template + CSS already started)*
2. **P2 — Photo uploader:** port the session **Photos** tab into the new session view; wrap existing upload routes with drag & drop + grid + progress. Biggest UX win.
3. **P3 — Clients CRM:** new `/admin/v2/clients` two-pane, **server-side pagination** (current page renders all 1,713 — add `LIMIT/OFFSET` + `?page=`).
4. **P4 — Parity pass:** port remaining actions (confirm/cancel/reschedule/invoice/Stripe/manual-book/blocking) reusing existing JS handlers.
5. **P5 — Swap:** point `/admin` at the new templates, rename old to `*_legacy`, update tests that assert on old markup, keep a `?legacy=1` escape hatch for one release.

Each P is independently shippable and leaves the app working.

---

## 5. Data contracts (exact fields — verified in app.py)

### `/admin` handler (`app.py` ~line 6630) passes:
- `bookings`: list of dict rows. Fields: `id, name, email, phone, instagram, date, time, session_type, status, paid_amount` + injected `selected_addons`, `addons_total`. **status values:** `confirmed`, `pending_payment`, `reserved`, `cancelled`, `expired`.
- `filtered_stats` / `overall_stats`: `{total, confirmed, pending, cancelled, expired, total_expected}`.
- `events` = `EVENTS` (full event objects, used for the edit cards: schedule/info/included/addons/photos).
- `event_summaries`: list of `{id, title, date, start_time, end_time, location, status, booked, confirmed, pending, free, occupancy, attention_count, is_future, photos}`. `photos` = list of image URL strings.
- `next_event`: one summary or `None`.
- `session_types`: list of strings.
- `now`: local datetime.
- `filters`: `{date_from, date_to, session_type, status, search, page, limit, total_count, total_pages}`. Query params drive server-side filtering/pagination already — reuse them.

### Clients API
- `GET /admin/api/clients?q=&tag=&sort=` → list of `{id, name, email, phone, instagram, tags, total_bookings, total_confirmed, total_paid, first_booking_at, last_booking_at, created_at, notes}`. `tags` is a comma string. `sort` ∈ `{last_booking_at, total_bookings, total_paid, name, created_at}`. **No pagination yet — add it (P3).**
- `GET /admin/api/clients/<id>` → `{client, bookings[], notes[]}` (notes normalized to `text`).
- `POST /admin/api/clients/<id>/note` `{text}`; `DELETE .../note/<note_id>`; `POST .../tag` `{tag}`; `POST .../edit`.
- Tag vocabulary: `VIP, FAMILY, COUPLE, RETURNING, NEW, CORPORATE`.

---

## 6. Test hooks to preserve (from `tests/`)

Run `python3 -m pytest tests/ -q` (expect 59 passed). Admin-touching tests:
- `test_admin_event_organizer.py`, `test_admin_event_manual_book.py`, `test_admin_event_price_updates.py`
- `test_admin_photo_uploads.py` — **photo routes must keep behaviour/shape.**
- `test_admin_transfers.py` — asserts literal strings on `/admin/transfers` (`"Yulia Levitskaya"`, `"$120.50"`, `"unmatched"`). **Do not restyle /admin/transfers markup in a way that drops these.**
- `test_booking_detail_template.py` — asserts `href="/admin/clients"` exists in booking detail; auth redirect to `/admin/login`.
- `test_error_handlers_and_admin_auth.py`, `test_telegram_admin_whitelist.py`, `test_regression_known_bugs.py` (`/admin/confirm`, `/admin/api/clients/<id>/note`).

**Implication:** because tests pin old markup, keep legacy routes intact and add the new UI in parallel. When you finally swap (P5), update these assertions in the same commit.

---

## 7. Admin route inventory (existing — reuse, don't recreate)

```
/admin (GET)                         dashboard
/admin/login /logout /health
/admin/export  /admin/analytics  /admin/link-generator  /admin/analytics.csv
/admin/booking/<id> (GET)            booking detail
/admin/booking/<id>/contact|invoice|send-invoice|recheck-payment|wfolio|send-review|no-show (POST)
/admin/confirm  /cancel  /mark-paid  /delete  /reschedule  /request-balance (POST)
/admin/events/create
/admin/events/<event_id>/update|delete|duplicate|update-meta (POST)
/admin/photos/<event_id> (GET)
/admin/photos/<event_id>/upload (POST)        single, field "photo", optional index
/admin/photos/<event_id>/upload-batch (POST)  multiple
/admin/photos/<event_id>/delete (POST)        {index}
/admin/transfers  /transfers/import  /auto-link  /<id>/link|ignore|unlink
/admin/clients (GET page)
/admin/event/<event_id> (GET page)            session detail (admin_event.html)
/admin/api/event/<id>/slots|block-slot|unblock-slot|block-day|manual-book
/admin/api/clients ... (see §5)
/admin/api/generate-invoice  /api/private-session  /backup  /backups  /api/clients/export
```

Existing JS handlers in `admin.html` to reuse by name: `openStripeModal, openCreateModal, openPrivateSessionModal, switchTab(evId,tab), uploadPhoto(evId,index,input), deletePhoto(evId,index), createStripeLink, copyStripeLink`. Clients page (`admin_clients.html`): `openClient, toggleTag, setTagFilter, debounceSearch`.

---

## 8. Component mapping: mock → real

| Mock (preview) | Real template | Backend it must call |
|---|---|---|
| Left **sidebar** (Dashboard/Sessions/Clients/Tools) | new shared partial / base | static links to existing routes |
| Dashboard **KPI cards** | `admin_pro.html` | `filtered_stats` / `overall_stats` |
| **Sessions grid** w/ occupancy bar | `admin_pro.html` | `event_summaries`; "Open" → `/admin/event/<id>` |
| **Recent bookings** table + chips + search + pager | `admin_pro.html` | `bookings`, `filters` (server-side via query params already supported) |
| Booking row actions Confirm/Open | reuse | `POST /admin/confirm`, `/admin/booking/<id>` |
| Session **Photos** dropzone + grid | new session view | `POST /admin/photos/<id>/upload-batch` & `/delete` (see §9) |
| Session **Slot board** / roster | new session view | `/admin/api/event/<id>/slots`, `/manual-book` |
| **Clients** two-pane + tag chips + pager | `/admin/v2/clients` | `/admin/api/clients` (add pagination), `/<id>` detail, `/note`, `/tag` |

---

## 9. Photo uploader spec (the key win)

Current (`admin.html` ~line 1862): a `+ Add photos` file input, hover-only Replace/Delete, "up to 5", first = Main.

Target (see mock `#dz` / `.pgrid`):
- Large **drag & drop** zone; full-zone click-to-browse; `multiple`.
- Thumbnail **grid**: `Main` badge on first; **drag-to-reorder** to set main; Replace + Delete **always reachable** (not hover-only — fails on touch); per-tile **progress bar**.
- Visible **counter** `N / MAX`.
- **Wire to existing routes**: batch → `POST /admin/photos/<event_id>/upload-batch`; delete → `POST /admin/photos/<event_id>/delete {index}`; reorder → needs a small new endpoint OR reuse update-meta to persist order (check `app.py` ~8246–8415 before adding anything).
- **MAX:** backend currently implies 5. Confirm the real cap in `app.py` before showing 8; keep UI and backend in sync. Don't raise the limit without checking storage/`_admin_event_summaries`.

---

## 10. Definition of done per phase

- App boots; `/admin` unchanged; `/admin/v2` renders new dashboard with **real** data.
- `pytest tests/ -q` → 59 passed.
- No new secrets/logs; no DB writes outside existing endpoints.
- Mobile: sidebar collapses (mock already handles `@media`).

## 11. Quick start
```bash
cd /Users/andrzej/Iryna-Master/01-Booking-System
python3 -m pytest tests/ -q        # expect 59 passed
# open admin-redesign-preview.html in a browser = the approved target
```
