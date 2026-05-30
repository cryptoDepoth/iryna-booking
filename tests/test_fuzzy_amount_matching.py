"""Task 5 — fuzzy e-Transfer amount matching (money-critical).

Guards the payment decision in ``check_etransfer_v2.match_by_amount_only``:
  * exact / overpayment  -> auto-confirm (records the ACTUAL amount)
  * underpayment         -> record only, never auto-confirm
  * two candidates in the same band -> ambiguous (manual), never confirm the wrong one
And the ``record_partial_payment`` safety guard (only ever touches unconfirmed rows).
"""
import sqlite3

import pytest

import check_etransfer_v2 as M


@pytest.fixture
def matcher(monkeypatch):
    EXP, FULL = {}, {}
    monkeypatch.setattr(M, "get_expected_amount_for_booking", lambda bid: EXP[bid])
    monkeypatch.setattr(M, "_get_booking_full_price", lambda bk: FULL[bk["id"]])

    def run(amount, specs):
        # specs: list of (id, expected_deposit, full_price)
        for i, e, f in specs:
            EXP[i], FULL[i] = e, f
        bookings = [{"id": i, "name": i, "event_id": i} for i, _, _ in specs]
        b, amb, mt = M.match_by_amount_only(amount, bookings)
        return (b["id"] if b else None, sorted(x["id"] for x in amb), mt)

    return run


def test_exact_single_confirms(matcher):
    assert matcher(110, [("a", 110, 200)]) == ("a", [], "exact")


def test_exact_duplicate_is_ambiguous(matcher):
    assert matcher(110, [("a", 110, 200), ("b", 110, 200)]) == (None, ["a", "b"], None)


def test_overpayment_auto_confirms(matcher):
    # client owed $110, sent $120 — the headline case
    assert matcher(120, [("a", 110, 200)]) == ("a", [], "overpaid")


def test_full_price_paid_upfront_is_overpaid(matcher):
    assert matcher(200, [("a", 110, 200)]) == ("a", [], "overpaid")


def test_overpayment_beyond_full_price_is_orphan(matcher):
    # $250 > full price $200 -> too much, stay manual
    assert matcher(250, [("a", 110, 200)]) == (None, [], None)


def test_underpayment_records_only(matcher):
    assert matcher(80, [("a", 110, 200)]) == ("a", [], "underpaid")


def test_tiny_underpayment_is_orphan(matcher):
    # $30 is < 30% of $110 -> not a credible partial payment
    assert matcher(30, [("a", 110, 200)]) == (None, [], None)


def test_two_overpaid_candidates_are_ambiguous(matcher):
    assert matcher(120, [("a", 100, 200), ("b", 110, 200)]) == (None, ["a", "b"], None)


def test_two_underpaid_candidates_are_ambiguous(matcher):
    assert matcher(90, [("a", 110, 200), ("b", 120, 200)]) == (None, ["a", "b"], None)


def test_overpaid_beats_underpaid_cross_band(matcher):
    # $120 overpays a($110) and underpays c($200) -> overpaid wins (higher confidence)
    assert matcher(120, [("a", 110, 200), ("c", 200, 400)]) == ("a", [], "overpaid")


def test_no_bookings_is_orphan(matcher):
    assert matcher(110, []) == (None, [], None)


def test_record_partial_payment_only_touches_unconfirmed(monkeypatch, tmp_path):
    dbfile = str(tmp_path / "bookings.db")

    def get_db():
        conn = sqlite3.connect(dbfile)
        conn.row_factory = sqlite3.Row
        return conn

    seed = get_db()
    seed.execute(
        "CREATE TABLE bookings (id INTEGER PRIMARY KEY, confirmed INT, paid INT, status TEXT, paid_amount REAL)"
    )
    seed.execute("INSERT INTO bookings VALUES (1, 0, 0, 'reserved', NULL)")    # still pending
    seed.execute("INSERT INTO bookings VALUES (2, 1, 1, 'confirmed', 110.0)")  # already confirmed
    seed.commit()
    seed.close()

    monkeypatch.setattr(M, "get_db", get_db)

    # Pending booking -> recorded as partial_payment
    assert M.record_partial_payment(1, 80.0) is True
    # Confirmed booking -> guard refuses to touch it
    assert M.record_partial_payment(2, 80.0) is False

    chk = get_db()
    r1 = chk.execute("SELECT status, paid_amount FROM bookings WHERE id=1").fetchone()
    r2 = chk.execute("SELECT status, paid_amount, confirmed FROM bookings WHERE id=2").fetchone()
    chk.close()

    assert r1["status"] == "partial_payment" and r1["paid_amount"] == 80.0
    # confirmed booking untouched — not flipped back to unpaid
    assert r2["status"] == "confirmed" and r2["confirmed"] == 1 and r2["paid_amount"] == 110.0
