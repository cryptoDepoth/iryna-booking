## [2026-05-16] — Booking Detail Card + Invoice + Gallery + Google Review

### Deployed
- App: iryna-booking
- Domain: https://book.pashynskaphoto.com/
- All tests: 115 passed, 1 skipped ✅
- Previous backup: .bak.2026-05-16/

### New Endpoints
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/admin/booking/<id>` | GET | Detail page with client, session, payments, actions |
| `/admin/booking/<id>/invoice` | POST | Generate branded PDF invoice |
| `/admin/booking/<id>/send-invoice` | POST | Send invoice email to client |
| `/admin/booking/<id>/wfolio` | POST | Save Wfolio URL + send gallery email |
| `/admin/booking/<id>/send-review` | POST | Send Google Review request email |

### New Template
- `templates/booking_detail.html` — standalone admin card page

### DB Changes
- Added column: `bookings.wfolio_url TEXT DEFAULT NULL`
- Migration: `_migrate_db_v2()` runs on startup automatically

### Files Changed
- `app.py` — 5 new routes, ReportLab PDF, email templates, imports
- `templates/admin.html` — client name now links to detail page
- `templates/booking_detail.html` — new
- `requirements.txt` — `reportlab==4.3.1`
- `tests/test_admin_booking_detail.py` — 7 new tests

### Known Issues / TODO
- PDF requires `pip install reportlab` on deployment machine (included in requirements.txt)
- Gallery email not yet tested end-to-end with real Wfolio URL
