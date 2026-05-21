# Nightly QA System — Pashynska Booking

Automated multi-layer testing that runs 2-3 times per night.

## Architecture

```
qa/nightly/
├── scripts/
│   ├── nightly_test_runner.sh    # Main orchestration
│   └── cleanup.sh                # Manual cleanup
├── tests/
│   ├── layer1_business_logic/    # CRITICAL: reserve, confirm, cancel, expire
│   ├── layer2_api_database/      # Health, consistency, response times
│   ├── layer3_ui_e2e/            # Playwright browser tests
│   └── layer4_visual/            # Screenshot diff (minimal)
├── snapshots/
│   ├── baseline/                 # Reference screenshots
│   └── current/                  # Fresh screenshots
├── logs/                         # Per-run logs
└── reports/                      # JSON reports
```

## Setup

1. Install dependencies:
```bash
cd /Users/andrzej/business/iryna/iryna-booking
/Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m pip install   pytest pytest-playwright playwright pillow numpy requests python-dotenv
playwright install chromium
```

2. Create env file:
```bash
cp qa/nightly/.env.qa.example .env.qa
# Edit .env.qa with real values
```

3. Create baseline snapshots (first run):
```bash
pytest qa/nightly/tests/layer4_visual/test_layer4.py -v
# Will save baseline screenshots
```

## Run Now (Manual)

```bash
./qa/nightly/scripts/nightly_test_runner.sh
```

Or individual layers:
```bash
pytest qa/nightly/tests/layer1_business_logic/test_layer1.py -v
pytest qa/nightly/tests/layer2_api_database/test_layer2.py -v
pytest qa/nightly/tests/layer3_ui_e2e/test_layer3.py -v
pytest qa/nightly/tests/layer4_visual/test_layer4.py -v
```

## Schedule (Cron)

```bash
# Via Hermes cronjob
hermes cronjob create   --name booking-nightly-qa   --schedule '0 1,3,5 * * *'   --script qa/nightly/scripts/nightly_test_runner.sh   --workdir /Users/andrzej/business/iryna/iryna-booking
```

Or via system cron:
```bash
0 1,3,5 * * * cd /Users/andrzej/business/iryna/iryna-booking && ./qa/nightly/scripts/nightly_test_runner.sh >> /tmp/nightly_cron.log 2>&1
```

## Notifications

- **Failure:** Telegram alert with failing layer + log path
- **Success:** Concise "All green" message
- Configure: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env.qa`

## Production Safety

- All QA bookings use `qa-test@example.com` pattern
- Automatic cleanup after every run
- Gmail bounce messages auto-deleted
- Snapshots cleaned before next run

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Playwright not found | `playwright install chromium` |
| DB locked | Stop local server, retry |
| Tests fail on first run | Create baseline snapshots first |
| Telegram not sending | Check `TELEGRAM_BOT_TOKEN` |

## Layers Explained

**Layer 1 — Business Logic (MANDATORY)**
- Reserve slot → slot unavailable
- Confirm → status persists
- Cancel → slot available
- Expire → cleanup
- Duplicate prevention
- Data integrity

**Layer 2 — API + Database**
- Health endpoint <500ms
- Events/slots structure
- No orphan bookings
- No double-confirmed slots
- Monotonic IDs

**Layer 3 — UI/E2E**
- Landing renders
- Drawer opens
- Form submits
- Payment page loads
- Admin login + dashboard
- Mobile viewport

**Layer 4 — Visual (OPTIONAL)**
- Landing screenshot
- Drawer screenshot
- Catastrophic layout changes only
- Threshold: 100 pixels

## Recommendations

### Immediate
- [ ] Fix `filtered_stats` bug (admin.html line 548)
- [ ] Add healthcheck endpoint `/health` with DB status
- [ ] Add rate limiting to `/reserve` endpoint

### Short-term
- [ ] Add `created_at` index for faster queries
- [ ] Add `status` index for admin dashboard
- [ ] Implement DB snapshot before nightly tests
- [ ] Add retry logic for flaky network in Layer 3

### Monitoring
- [ ] Track booking creation rate (alerts if zero)
- [ ] Track admin login failures
- [ ] Track e-Transfer checker uptime
