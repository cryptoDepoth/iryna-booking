# Production Deploy Source of Truth

This folder is the canonical production source for Iryna Pashynska Photography booking:

```text
/Users/andrzej/Iryna-Master/01-Booking-System
```

Production app:

```text
Fly app: iryna-booking
Domain: https://book.pashynskaphoto.com/
Apex redirect: https://pashynskaphoto.com/ → https://book.pashynskaphoto.com/
```

Safe deploy checklist:

```bash
cd /Users/andrzej/Iryna-Master/01-Booking-System
python -m pytest -q
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
```

That path is an old/stale clone and is explicitly marked `DO_NOT_DEPLOY_THIS_OLD_CLONE.md`.
