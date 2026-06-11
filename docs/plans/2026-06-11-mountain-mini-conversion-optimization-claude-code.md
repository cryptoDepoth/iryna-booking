# Mountain Mini Conversion Optimization — Claude Code Handoff

> **For Claude Code:** implement small, safe booking-site conversion improvements. Do not change Meta Ads via API. Do not alter payment flow behavior except for instrumentation/copy improvements. Preserve existing routes and tests.

**Goal:** Improve conversion from Meta website traffic for Pashynska Photography Mountain Mini sessions: visitors currently open the booking drawer and sometimes reserve, but confirmed paid bookings are still 0.

**Current evidence (2026-06-11):**
- Website ad `120244741799840408` is ACTIVE.
- Last 7d website ad: ~$75.41 spend, 372 LPV, 402 link clicks, 3 messaging conversations.
- Booking analytics mountain funnel includes: 575 visits, 95 drawer opens, 12 slot selections, 4 reserve attempts, 2 bookings, 0 confirmed.
- Current live ad URL: `https://book.pashynskaphoto.com/?event=mountain_mini&utm_source=instagram&utm_medium=paid&utm_campaign=mountain_mini_jun20`
- Preferred clean future URL for new/edited ads: `https://book.pashynskaphoto.com?event=mountain_mini&utm_source=meta&utm_medium=cpc&utm_campaign=mountain_mini_2026&utm_content=website_ad_v2`

## Worktree

Repo: `/Users/andrzej/Iryna-Master/01-Booking-System`

Run before changes:
```bash
cd /Users/andrzej/Iryna-Master/01-Booking-System
python3 -m pytest
```
Expected currently: full suite green (`358 passed, 1 skipped` was last observed after abandoned follow-up work).

## Priority 1 — Make Mountain Mini offer obvious above the fold

**Problem:** Traffic clicks and opens page, but many do not select slots. The first 5 seconds must answer: price, date, location, what is included, how to reserve.

**Implement:**
- On `event=mountain_mini`, show a compact hero block near top / booking section:
  - `Mountain Mini Sessions`
  - `June 20 & July 11 near Calgary`
  - `$250 + GST · 30 minutes · 20 retouched photos`
  - `All original photos + short video included`
  - `Reserve your time — limited spots`
- Make primary CTA explicit: `Choose a time` / `Reserve my spot`.
- Keep copy short and mobile-first.

**Files to inspect/modify:**
- `templates/index.html`
- `static/*` CSS/JS if needed
- `app.py` only if template context/event copy needs backend support

**Tests:**
- Add/adjust template or route test verifying Mountain Mini page contains price, dates, included deliverables, and CTA.

## Priority 2 — Reduce confusion at slot/payment step

**Problem:** There are reserve attempts/bookings but 0 confirmed. Users may not understand whether they must pay now, how much deposit is required, or what happens after reserve.

**Implement:**
- Near slot selection / reserve form, add plain language:
  - `To secure your time, complete the payment after reserving.`
  - `Your spot is held temporarily until payment is completed.`
  - `Questions? Message Iryna on Instagram @pashynska.photo.`
- On abandoned/unpaid booking confirmation state, make status clear: `Reserved but not confirmed until payment`.
- Do not change business logic unless tests prove copy-only is insufficient.

**Tests:**
- Verify unpaid/reserve state text appears where relevant.
- Existing abandoned email tests must remain green.

## Priority 3 — Improve funnel instrumentation names

**Problem:** Analytics are split between `mountain_mini_booking_ab_202606`, `mountain_mini_jun20`, and future clean UTM. This makes decisions harder.

**Implement:**
- Do not rewrite historical rows.
- Add a canonical reporting grouping in admin analytics if low-risk: campaigns containing `mountain_mini` can be summarized under a single display group `Mountain Mini` while preserving raw campaign/content columns.
- If too risky, add a separate helper/report endpoint or script rather than changing existing CSV.

**Tests:**
- Analytics CSV still has existing columns.
- New grouping does not break old rows.

## Priority 4 — Add a no-risk internal QA link

Create/confirm a QA URL for manual testing:
```text
https://book.pashynskaphoto.com?event=mountain_mini&utm_source=qa&utm_medium=manual&utm_campaign=mountain_mini_qa&utm_content=claude_code_verify
```

After deploy, verify:
- HTTP 200
- UTM preserved in browser URL
- Hero copy visible on mobile width
- CTA opens/scrolls to available slots
- Slot selection event records
- Reserve attempt records

## Do NOT do without explicit approval

- Do not pause, edit, or duplicate live Meta Ads.
- Do not change payment provider settings.
- Do not send emails to clients manually.
- Do not change prices.
- Do not remove private/individual photoshoot functionality.

## Suggested implementation order

1. Read `templates/index.html`, `app.py`, and relevant tests.
2. Add failing tests for Mountain Mini copy visibility.
3. Implement mobile-first copy/CTA changes.
4. Run targeted tests.
5. Add/adjust tests for unpaid/reserve clarity copy.
6. Implement copy changes.
7. Run full test suite.
8. If green, commit:
```bash
git add templates static tests app.py docs/plans/2026-06-11-mountain-mini-conversion-optimization-claude-code.md
git commit -m "feat(booking): clarify mountain mini conversion flow"
```

## Success criteria

- Full tests green.
- QA URL returns 200.
- Mobile page clearly shows price/date/location/includes/CTA without hunting.
- Booking funnel events still work.
- No live Meta Ads changed.
