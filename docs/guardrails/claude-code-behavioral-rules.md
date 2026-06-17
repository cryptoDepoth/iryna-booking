# Hermes → Claude Code: Behavioral Rules & Task Guardrails

> **For Claude Code:** Follow these rules strictly. They define your behavior, priorities, and boundaries for Pashynska Photography tasks.

## Core Principles

1. **No Surprises**
   - Never change live Meta Ads, payment flow, or booking logic without explicit approval.
   - Never pause, edit, or duplicate live ads.
   - Never send emails to clients manually.
   - Never change prices or deposit logic.

2. **Safety First**
   - Always run full test suite before commit:
     ```bash
     cd /Users/andrzej/Iryna-Master/01-Booking-System
     python3 -m pytest
     ```
   - Expected: **372+ passed, 0 failed** (last observed: 372 passed after Fable 5 work).
   - If tests fail, fix or ask Hermes for clarification — do not proceed.

3. **Small, Safe Changes**
   - One logical change per commit.
   - Prefer copy/text changes over logic changes.
   - Prefer CSS/JS tweaks over backend changes.

4. **Mobile-First**
   - Assume 80% of traffic is mobile.
   - Test changes on mobile viewport (375px width).
   - Keep copy short and scannable.

5. **Russian Tone**
   - Use warm, personal, conversational Russian.
   - Avoid corporate/formal tone.
   - Example:
     ❌ «Для бронирования нажмите кнопку»
     ✅ «Выберите удобное время — я помогу с бронированием»

6. **No New Features**
   - Do not add new booking types, payment methods, or admin features.
   - Do not add new dependencies.
   - Do not add new cron jobs or background tasks.

7. **No Data Loss**
   - Never delete or overwrite existing bookings, clients, or analytics.
   - Never modify `.env` or secrets.
   - Never modify `fly.toml` or deployment config.

8. **No Live Experiments**
   - Do not A/B test on live traffic without Hermes approval.
   - Do not change UTM parameters on live ads.
   - Do not change ad creative or targeting.

## Task-Specific Rules

### Mountain Mini Conversion Optimization

**Goal:** Improve conversion from Meta website traffic: visitors open drawer, sometimes reserve, but confirmed paid bookings are still 0.

**Allowed:**
- Add/improve hero copy above the fold:
  - Price: `$250 + GST`
  - Dates: `June 20 & July 11 near Calgary`
  - What’s included: `30 min · 20 retouched photos · all originals · short video`
  - CTA: `Choose a time` / `Reserve my spot`
- Add payment clarity near reserve form:
  - `Your spot is held temporarily until payment is completed.`
  - `To secure your time, complete the payment after reserving.`
  - `Questions? Message Iryna on Instagram @pashynska.photo.`
- Improve unpaid/reserved state text:
  - `Reserved but not confirmed until payment`

**Files to touch:**
- `templates/index.html`
- `static/css/main.css` (only if mobile layout breaks)
- `static/js/main.js` (only if CTA event tracking breaks)

**Files to avoid:**
- `app.py` (unless template context requires it)
- `models.py`
- `routes.py`
- `fly.toml`
- `.env`
- `tests/` (unless adding targeted tests for new copy)

### Booking Funnel Instrumentation

**Allowed:**
- Add canonical reporting grouping in admin analytics if low-risk:
  - Group campaigns containing `mountain_mini` under display name `Mountain Mini`.
  - Preserve raw `campaign`/`content` columns.
- If too risky, add a separate helper/report endpoint or script instead of changing existing CSV.

**Not allowed:**
- Rewrite historical analytics rows.
- Change existing CSV schema.

### QA & Testing

**QA URL:**
```text
https://book.pashynskaphoto.com?event=mountain_mini&utm_source=qa&utm_medium=manual&utm_campaign=mountain_mini_qa&utm_content=claude_code_verify
```

**Verify after deploy:**
- HTTP 200
- UTM preserved in browser URL
- Hero copy visible on mobile width
- CTA opens/scrolls to available slots
- Slot selection event records
- Reserve attempt event records

## Commit & Review

**Commit message format:**
```text
feat(booking): clarify mountain mini conversion flow [skip ci]
```

**Before commit:**
```bash
cd /Users/andrzej/Iryna-Master/01-Booking-System
python3 -m pytest
```

**After commit:**
- Push to `main` branch.
- Hermes will auto-deploy to Fly.io.
- Verify QA URL after deploy.

## Escalation

If you encounter:
- Test failures
- Deployment issues
- Unclear requirements
- Safety concerns

**Do not proceed.**
Ask Hermes for clarification:
```text
@Hermes: clarification needed — [brief description of issue]
```

## Summary

```text
Small, safe, mobile-first copy changes only.
No logic, no ads, no payments, no new features.
Test before commit. Deploy after green tests.
```