# DOX: Tests

## Purpose

Booking system test suite. 352+ regression tests covering booking flow, timezone math, payment logic, admin endpoints, private sessions, calendar sync.

## Ownership

- `test_booking_flow.py` — core booking flow tests
- `test_private_session_flow.py` — private session tests
- `test_admin.py` — admin dashboard tests
- `test_gcal.py` — Google Calendar sync tests
- `qa/qa/` — live smoke tests

## Local Contracts

- `pytest` must pass 354 tests before deploy (was 352 before timezone fix)
- 2 existing tests may fail from aware/naive datetime mixing — needs `import timezone` in `test_booking_flow.py` + timer fix in `test_private_session_flow.py`
- Full deploy blocked until 354 tests green

## Work Guidance

- Test behavior, not implementation. Assert return values and side effects.
- Mock only at system boundaries: network, DB, calendar API.
- Every test must justify its existence: "What bug does this catch that no other test catches?"
- Production regression tests are sacred — reference issue ID or date.

## Verification

- `pytest` from project root
- `hermes live-qa` for smoke tests

## Child DOX Index

- None. Tests live at flat depth.
