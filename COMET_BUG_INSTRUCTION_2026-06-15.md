# Comet Bug Fix Instructions — Booking System

## Two reported bugs

1. **Photo upload to an event no longer works**
2. **Newly created event is not visible after creation** ← root cause found and patched in `app.py`; needs deploy and verification

## Reproduction steps (do these first in the browser)

### Bug 1 — Photos
1. Log in to `/admin`
2. Open any event page, e.g. `/admin/event/mountain-mini-session-2026-07-11`
3. Scroll to the **Photos** section
4. Try to upload one photo, then try a batch of 2–3 photos
5. Watch the browser **Console** and **Network** tab
6. Note the exact error message / HTTP status / toast shown

### Bug 2 — New event invisible
1. In `/admin`, click **Create new event**
2. Fill title, date (future), set **Initial status = Active (bookable now)**
3. Save
4. After reload, check:
   - Does the event appear in the admin session grid?
   - Does it appear on the public homepage `/`?
   - Does it appear in the `/events` API response?
   - What `status`, `hidden`, `session_type` values does it have?

## Files to inspect and likely fixes

### `app.py`

- **Photo upload functions** (around lines 8247–8366)
  - `admin_upload_photo()`
  - `admin_upload_photos_batch()`
  - `_save_optimized_admin_photo()` (around line 8083)
  - `_delete_photo_file()`

- **Event creation** (around lines 8419–8518)
  - `admin_create_event()`
  - Check default `status`: it defaults to `"upcoming"` if not sent from the form
  - `_reload_events_globals()`

- **Static photo serving** (around line 2514)
  - Route that serves `/uploaded/...` or `/images/...`
  - Check `UPLOAD_DIR`, `PHOTO_DIR`, and whether the Fly volume path is correct

### `templates/admin.html`

- **Photo upload JavaScript** (around lines 2574–2642)
  - `uploadPhoto()`
  - `deletePhoto()`
  - Check that the fetch URL is correct: `/admin/photos/{eventId}/upload-batch`

- **Create event modal JavaScript** (around lines 2896–3039)
  - `submitCreateEvent()`
  - Verify `status` field is collected and sent (`document.getElementById('new-status').value`)

### `templates/index_v2.html`

- **Public event filtering** (around lines 1469–1519)
  - `loadEvents()`
  - `visibleEvents()` filters `status !== "completed"`, so `upcoming` and `active` should show
  - If the new event has `status = "upcoming"` but is still missing, check the `/events` API response

## Diagnostic commands

Run these inside the Fly machine (`flyctl ssh console -a iryna-booking`):

```bash
# Check the events file
head -n 80 /app/data/events.yaml

# Check photo upload directory and permissions
ls -la /app/data/uploads/ 2>/dev/null || echo "no uploads dir"
ls -la /data/uploads/ 2>/dev/null || echo "no /data/uploads"
ls -la /app/static/uploaded/ 2>/dev/null || echo "no static/uploaded"

# Check server logs for recent errors
flyctl logs -a iryna-booking --recent
```

## Likely root causes to verify

### Photos
- Missing `UPLOAD_DIR` / `PHOTO_DIR` environment variable on Fly
- Route for serving uploaded photos is broken or pointing to wrong directory
- `_save_optimized_admin_photo()` fails silently due to missing PIL or wrong path
- Batch upload route changed but admin JS still calls the old single-upload route

### New event invisible
- Form default status is `"upcoming"` but public page or admin grid may be filtering it out
- `_reload_events_globals()` may not be reloading correctly after YAML write
- The new event is created with `hidden: true` or `status: "completed"` due to a frontend/backend mismatch
- Date is in the past or admin grid only shows events after today

## Deliverables

1. Fix both bugs
2. Run the existing test suite: `pytest`
3. Verify in browser:
   - Upload 1 photo and a batch of 3 photos to an event → success toast + reload shows photos
   - Create a new event with status **Active** → appears in admin grid and on public homepage
4. Report back:
   - What exactly was broken
   - Files changed
   - Screenshot or log line proving the fix
