# Production Deploy Source of Truth

This folder is the canonical production source for Iryna Pashynska Photography booking:

```text
/Users/andrzej/business/iryna/iryna-booking
```

Production app:

```text
Fly app: iryna-booking
Domain: https://book.pashynskaphoto.com/
Apex redirect: https://pashynskaphoto.com/ → https://book.pashynskaphoto.com/
```

Safe deploy checklist:

```bash
cd /Users/andrzej/business/iryna/iryna-booking
.venv/bin/python -m pytest -q
/Users/andrzej/.fly/bin/flyctl deploy --remote-only --yes -a iryna-booking
```

Post-deploy smoke checks:

```bash
curl -I https://book.pashynskaphoto.com/healthz
curl -I https://book.pashynskaphoto.com/events
curl -L https://pashynskaphoto.com/ | grep -E "Calgary Family|Pashynska Photography|Reserve|event-card" | head
```

Do not deploy from:

```text
/Users/andrzej/iryna-booking
/Users/andrzej/Iryna-Master/01-Booking-System
```

Those paths are stale/working copies. Use the canonical folder above unless this file is intentionally updated after a controlled migration.
