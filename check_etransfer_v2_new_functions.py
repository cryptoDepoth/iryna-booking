def record_partial_payment(booking_id, paid_amount):
    """Record a partial/underpayment without confirming the booking.
    
    Updates paid_amount on the booking but keeps confirmed=0, paid=0.
    Sets status to 'partial_payment' so admin can see it needs attention.
    """
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        UPDATE bookings
        SET paid_amount=?, status='partial_payment'
        WHERE id=?
    """, (paid_amount, booking_id))
    updated = c.rowcount
    conn.commit()
    conn.close()
    return updated > 0


def _notify_admin_underpaid(booking, expected, actual):
    """Notify admin: client paid less than expected, NOT auto-confirmed."""
    try:
        from app import _tg_message
        diff = expected - actual
        total = _get_booking_full_price(booking)
        balance = max(total - actual, 0)
        lines = [
            f"⚠️ **Payment received (UNDERPAID)**",
            f"",
            f"Booking #{booking['id']} — {booking.get('name', '?')}",
            f"Expected deposit: ${expected:.2f}",
            f"Received: ${actual:.2f}",
            f"Shortfall: -${diff:.2f}",
            f"",
            f"❌ NOT auto-confirmed",
            f"Remaining balance: ${balance:.2f}",
            f"",
            f"🔗 [View Booking](https://book.pashynskaphoto.com/admin/booking/{booking['id']})",
        ]
        _tg_message("\n".join(lines))
    except Exception as e:
        print(f"[admin] Failed to send underpaid alert: {e}")


def _notify_admin_overpaid(booking, expected, actual):
    """Notify admin: client paid more than expected, auto-confirmed."""
    try:
        from app import _tg_message
        diff = actual - expected
        total = _get_booking_full_price(booking)
        balance = max(total - actual, 0)
        lines = [
            f"💰 **Payment received (overpaid)**",
            f"",
            f"Booking #{booking['id']} — {booking.get('name', '?')}",
            f"Expected deposit: ${expected:.2f}",
            f"Received: ${actual:.2f}",
            f"Extra: +${diff:.2f}",
            f"",
            f"✅ Auto-confirmed",
            f"Remaining balance: ${balance:.2f}",
        ]
        _tg_message("\n".join(lines))
    except Exception as e:
        print(f"[admin] Failed to send overpaid alert: {e}")