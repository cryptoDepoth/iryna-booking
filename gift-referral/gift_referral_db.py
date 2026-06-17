"""
Gift Certificates + Referral Codes — database layer.
All queries go through get_db(); caller is responsible for no further imports.
"""

import os
import secrets
import sqlite3
import string
from datetime import datetime, timedelta, timezone

from gift_referral_catalog import GIFT_PACKAGES, GST_RATE, calculate_with_gst

DB_PATH = os.environ.get("GIFT_REFERRAL_DB", os.path.join(os.path.dirname(__file__), "gift_referral.db"))

PACKAGES = {
    key: {
        "label": value["label"],
        "duration": value["duration"],
        "photos": value["photos"],
        "amount": value["amount"],
        "amount_with_gst": calculate_with_gst(value["amount"]),
    }
    for key, value in GIFT_PACKAGES.items()
}


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | None = None) -> None:
    global DB_PATH
    if db_path:
        DB_PATH = db_path
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS gift_certificates (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            code                TEXT UNIQUE NOT NULL,
            purchaser_email     TEXT NOT NULL,
            purchaser_name      TEXT NOT NULL,
            recipient_name      TEXT,
            recipient_email     TEXT,
            personal_message    TEXT,
            session_type        TEXT,
            amount              REAL NOT NULL,
            amount_with_gst     REAL NOT NULL,
            custom_base         TEXT DEFAULT '',
            certificate_style   TEXT DEFAULT 'signature',
            package_label       TEXT DEFAULT '',
            addons_json         TEXT DEFAULT '[]',
            photo_url           TEXT DEFAULT '',
            payment_method      TEXT DEFAULT 'stripe',
            payment_status      TEXT DEFAULT 'paid',
            paid_amount         REAL DEFAULT 0,
            payment_reference   TEXT,
            activated_at        TEXT,
            stripe_payment_intent TEXT,
            stripe_session_id   TEXT,
            status              TEXT DEFAULT 'active',
            created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at          TEXT,
            redeemed_at         TEXT,
            redeemed_booking_id INTEGER,
            pdf_path            TEXT,
            pdf_sent            INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS referral_codes (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            code                TEXT UNIQUE NOT NULL,
            owner_email         TEXT NOT NULL,
            owner_name          TEXT NOT NULL,
            owner_booking_id    INTEGER,
            discount_for_friend REAL DEFAULT 20.0,
            reward_for_owner    REAL DEFAULT 20.0,
            uses_count          INTEGER DEFAULT 0,
            max_uses            INTEGER DEFAULT 10,
            owner_self_used     INTEGER DEFAULT 0,
            status              TEXT DEFAULT 'active',
            created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at          TEXT
        );

        CREATE TABLE IF NOT EXISTS referral_uses (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            referral_code_id    INTEGER NOT NULL,
            referee_email       TEXT NOT NULL,
            referee_name        TEXT,
            referee_booking_id  INTEGER,
            discount_applied    REAL,
            payment_confirmed   INTEGER DEFAULT 0,
            reward_triggered    INTEGER DEFAULT 0,
            reward_booking_id   INTEGER,
            created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
            confirmed_at        TEXT,
            FOREIGN KEY (referral_code_id) REFERENCES referral_codes(id)
        );

        CREATE TABLE IF NOT EXISTS referral_credits (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_email  TEXT UNIQUE NOT NULL,
            owner_name   TEXT NOT NULL,
            total_earned REAL DEFAULT 0.0,
            balance      REAL DEFAULT 0.0,
            created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at   TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS referral_credit_events (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_email      TEXT NOT NULL,
            event_type       TEXT NOT NULL,
            amount           REAL NOT NULL,
            referral_use_id  INTEGER,
            booking_id       INTEGER,
            note             TEXT,
            created_at       TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    _ensure_gift_columns(conn)
    _ensure_referral_columns(conn)
    conn.execute("""
        UPDATE gift_certificates
           SET payment_status=COALESCE(payment_status, 'paid'),
               payment_method=COALESCE(payment_method, 'stripe'),
               paid_amount=CASE
                   WHEN paid_amount IS NULL AND status='active' THEN amount_with_gst
                   WHEN paid_amount IS NULL THEN 0
                   ELSE paid_amount
               END
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _random_chars(n: int) -> str:
    return "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(n))


def calculate_gst(amount: float) -> float:
    return calculate_with_gst(amount)


def _ensure_gift_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(gift_certificates)").fetchall()}
    migrations = {
        "custom_base": "ALTER TABLE gift_certificates ADD COLUMN custom_base TEXT DEFAULT ''",
        "certificate_style": "ALTER TABLE gift_certificates ADD COLUMN certificate_style TEXT DEFAULT 'signature'",
        "package_label": "ALTER TABLE gift_certificates ADD COLUMN package_label TEXT DEFAULT ''",
        "addons_json": "ALTER TABLE gift_certificates ADD COLUMN addons_json TEXT DEFAULT '[]'",
        "photo_url": "ALTER TABLE gift_certificates ADD COLUMN photo_url TEXT DEFAULT ''",
        "payment_method": "ALTER TABLE gift_certificates ADD COLUMN payment_method TEXT DEFAULT 'stripe'",
        "payment_status": "ALTER TABLE gift_certificates ADD COLUMN payment_status TEXT DEFAULT 'paid'",
        "paid_amount": "ALTER TABLE gift_certificates ADD COLUMN paid_amount REAL DEFAULT 0",
        "payment_reference": "ALTER TABLE gift_certificates ADD COLUMN payment_reference TEXT",
        "activated_at": "ALTER TABLE gift_certificates ADD COLUMN activated_at TEXT",
    }
    for column, ddl in migrations.items():
        if column not in columns:
            conn.execute(ddl)


def _ensure_referral_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(referral_codes)").fetchall()}
    migrations = {
        "owner_self_used": "ALTER TABLE referral_codes ADD COLUMN owner_self_used INTEGER DEFAULT 0",
    }
    for column, ddl in migrations.items():
        if column not in columns:
            conn.execute(ddl)


# ---------------------------------------------------------------------------
# Gift Certificates
# ---------------------------------------------------------------------------

def generate_gift_code() -> str:
    return f"GIFT-{_random_chars(4)}-{_random_chars(4)}"


def create_gift_certificate(
    purchaser_email: str,
    purchaser_name: str,
    recipient_name: str,
    recipient_email: str,
    personal_message: str,
    session_type: str,
    amount: float,
    amount_with_gst: float,
    custom_base: str = "",
    certificate_style: str = "signature",
    package_label: str = "",
    addons_json: str = "[]",
    photo_url: str = "",
    payment_method: str = "stripe",
    payment_status: str = "paid",
    paid_amount: float | None = None,
    payment_reference: str | None = None,
    status: str = "active",
    stripe_payment_intent: str | None = None,
    stripe_session_id: str | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=365)).strftime("%Y-%m-%d")
    if paid_amount is None:
        paid_amount = amount_with_gst if status == "active" else 0.0
    activated_at = now.strftime("%Y-%m-%d %H:%M:%S") if status == "active" else None
    for _ in range(20):
        code = generate_gift_code()
        conn = get_db()
        try:
            conn.execute(
                """INSERT INTO gift_certificates
                   (code, purchaser_email, purchaser_name, recipient_name, recipient_email,
                    personal_message, session_type, amount, amount_with_gst,
                    custom_base, certificate_style, package_label, addons_json, photo_url,
                    payment_method, payment_status, paid_amount, payment_reference,
                    activated_at, stripe_payment_intent, stripe_session_id, status, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    code, purchaser_email, purchaser_name, recipient_name, recipient_email,
                    personal_message, session_type, amount, amount_with_gst,
                    custom_base, certificate_style, package_label, addons_json, photo_url,
                    payment_method, payment_status, paid_amount, payment_reference,
                    activated_at, stripe_payment_intent, stripe_session_id, status, expires_at,
                ),
            )
            conn.commit()
            return code
        except sqlite3.IntegrityError:
            pass
        finally:
            conn.close()
    raise RuntimeError("Could not generate unique gift code after 20 attempts")


def get_gift_certificate(code: str) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM gift_certificates WHERE code = ?", (code,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_gift_certificate_by_stripe_session(stripe_session_id: str) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM gift_certificates WHERE stripe_session_id = ?", (stripe_session_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def validate_gift_certificate(code: str, session_type: str | None = None) -> dict:
    cert = get_gift_certificate(code)
    if not cert:
        return {"valid": False, "error": "Certificate not found"}
    if cert["status"] == "redeemed":
        return {"valid": False, "error": "This certificate has already been redeemed"}
    if cert["status"] in ("expired", "refunded"):
        return {"valid": False, "error": f"This certificate is {cert['status']}"}
    if cert["status"] != "active":
        return {"valid": False, "error": "This certificate is awaiting payment confirmation"}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if cert["expires_at"] and cert["expires_at"] < today:
        _set_gift_status(code, "expired")
        return {"valid": False, "error": "This certificate has expired"}
    if (
        session_type
        and cert["session_type"]
        and cert["session_type"] not in ("custom", "")
        and cert["session_type"] != session_type
    ):
        return {
            "valid": False,
            "error": f"This certificate is valid for a {cert['session_type']} session only",
        }
    return {
        "valid": True,
        "type": "gift",
        "session_type": cert["session_type"],
        "amount": cert["amount"],
        "amount_with_gst": cert["amount_with_gst"],
        "recipient_name": cert["recipient_name"],
        "expires_at": cert["expires_at"],
    }


def redeem_gift_certificate(code: str, booking_id: int) -> bool:
    conn = get_db()
    conn.execute(
        """UPDATE gift_certificates
           SET status = 'redeemed', redeemed_at = CURRENT_TIMESTAMP, redeemed_booking_id = ?
           WHERE code = ? AND status = 'active'""",
        (booking_id, code),
    )
    conn.commit()
    changed = conn.total_changes
    conn.close()
    return changed > 0


def _set_gift_status(code: str, status: str) -> None:
    conn = get_db()
    conn.execute("UPDATE gift_certificates SET status = ? WHERE code = ?", (status, code))
    conn.commit()
    conn.close()


def update_gift_pdf(code: str, pdf_path: str) -> None:
    conn = get_db()
    conn.execute(
        "UPDATE gift_certificates SET pdf_path = ?, pdf_sent = 1 WHERE code = ?",
        (pdf_path, code),
    )
    conn.commit()
    conn.close()


def mark_gift_payment_confirmed(code: str, paid_amount: float, payment_reference: str | None = None) -> bool:
    conn = get_db()
    conn.execute(
        """UPDATE gift_certificates
              SET status='active',
                  payment_status='paid',
                  paid_amount=?,
                  payment_reference=COALESCE(?, payment_reference),
                  activated_at=CURRENT_TIMESTAMP
            WHERE code=?
              AND status='pending_payment'
              AND ABS(amount_with_gst - ?) < 0.01""",
        (paid_amount, payment_reference, code, paid_amount),
    )
    conn.commit()
    changed = conn.total_changes
    conn.close()
    return changed > 0


def list_gift_certificates() -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM gift_certificates ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_pending_gift_certs_by_amount(amount: float) -> list[dict]:
    """
    Returns all pending gift certificates with the given amount (matched against
    amount_with_gst, i.e. what the buyer actually transfers).
    Used for e-Transfer amount-only matching when buyer didn't write the code in memo.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT code, purchaser_name, purchaser_email, recipient_name,
                   session_type, amount, amount_with_gst, created_at
            FROM gift_certificates
            WHERE status = 'pending_payment'
              AND ABS(amount_with_gst - ?) < 0.01
            ORDER BY created_at ASC
            """,
            (amount,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Referral Codes
# ---------------------------------------------------------------------------

def generate_referral_code() -> str:
    return f"REF-PASHYN-{_random_chars(4)}"


def create_referral_code(
    owner_email: str,
    owner_name: str,
    owner_booking_id: int | None = None,
    discount_for_friend: float = 20.0,
    reward_for_owner: float = 20.0,
) -> str:
    expires_at = (datetime.utcnow() + timedelta(days=365)).strftime("%Y-%m-%d")
    for _ in range(20):
        code = generate_referral_code()
        conn = get_db()
        try:
            conn.execute(
                """INSERT INTO referral_codes
                   (code, owner_email, owner_name, owner_booking_id,
                    discount_for_friend, reward_for_owner, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (code, owner_email, owner_name, owner_booking_id,
                 discount_for_friend, reward_for_owner, expires_at),
            )
            conn.commit()
            return code
        except sqlite3.IntegrityError:
            pass
        finally:
            conn.close()
    raise RuntimeError("Could not generate unique referral code after 20 attempts")


def get_referral_code(code: str) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM referral_codes WHERE code = ?", (code,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_referral_code_by_owner(owner_email: str) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM referral_codes WHERE owner_email = ? AND status = 'active' ORDER BY created_at DESC LIMIT 1",
        (owner_email,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def validate_referral_code(code: str, referee_email: str | None = None) -> dict:
    """
    Validates a referral code.
    - Friends (referee_email != owner): normal discount flow.
    - Owner using own code: credit_redemption flow if balance >= discount.
    """
    ref = get_referral_code(code)
    if not ref:
        return {"valid": False, "error": "Referral code not found"}
    if ref["status"] != "active":
        return {"valid": False, "error": "This referral code is no longer active"}
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if ref["expires_at"] and ref["expires_at"] < today:
        return {"valid": False, "error": "This referral code has expired"}
    if ref["uses_count"] >= ref["max_uses"]:
        return {"valid": False, "error": "This referral code has reached its maximum uses"}

    if referee_email and referee_email.lower().strip() == ref["owner_email"].lower().strip():
        # Owner using their own code — allowed once, no reward, discount only.
        if ref.get("owner_self_used"):
            return {
                "valid": False,
                "error": "You have already used your own referral code once.",
            }
        return {
            "valid": True,
            "type": "referral_self",
            "discount": ref["discount_for_friend"],
            "owner_name": ref["owner_name"],
            "code": code,
        }

    return {
        "valid": True,
        "type": "referral",
        "discount": ref["discount_for_friend"],
        "owner_name": ref["owner_name"],
        "code": code,
    }


def record_referral_use(
    code: str,
    referee_email: str,
    referee_name: str | None = None,
    referee_booking_id: int | None = None,
    discount_applied: float | None = None,
) -> int | None:
    ref = get_referral_code(code)
    if not ref:
        return None
    conn = get_db()
    is_owner_self_use = (
        referee_email and referee_email.lower().strip() == ref["owner_email"].lower().strip()
    )
    cursor = conn.execute(
        """INSERT INTO referral_uses
           (referral_code_id, referee_email, referee_name, referee_booking_id, discount_applied)
           VALUES (?, ?, ?, ?, ?)""",
        (ref["id"], referee_email, referee_name, referee_booking_id,
         discount_applied if discount_applied is not None else ref["discount_for_friend"]),
    )
    use_id = cursor.lastrowid
    # For owner self-use, only mark owner_self_used; do NOT consume friend uses or credit reward.
    # For normal friend uses, we intentionally do NOT increment uses_count here.
    # uses_count is incremented only after the friend's payment is confirmed
    # (in confirm_referral_payment), preventing abuse via pending reservations.
    if is_owner_self_use:
        conn.execute(
            "UPDATE referral_codes SET owner_self_used = 1 WHERE id = ?", (ref["id"],)
        )
    conn.commit()
    conn.close()
    return use_id


def confirm_referral_payment(referee_booking_id: int) -> dict | None:
    """
    Called when a friend's payment is confirmed.
    Marks the use as paid, adds +reward to owner's cumulative credit balance.
    Returns the use record enriched with owner info and updated credit balance.
    """
    conn = get_db()
    row = conn.execute(
        """SELECT ru.*, rc.owner_email, rc.owner_name, rc.reward_for_owner, rc.code as referral_code
           FROM referral_uses ru
           JOIN referral_codes rc ON ru.referral_code_id = rc.id
           WHERE ru.referee_booking_id = ? AND ru.payment_confirmed = 0""",
        (referee_booking_id,),
    ).fetchone()
    if not row:
        conn.close()
        return None
    use = dict(row)
    is_owner_self_use = (
        use.get("referee_email", "").lower().strip()
        == use.get("owner_email", "").lower().strip()
    )
    # Owner self-use gets the discount but never a reward.
    if is_owner_self_use:
        conn.execute(
            """UPDATE referral_uses
               SET payment_confirmed = 1, reward_triggered = 0, confirmed_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (use["id"],),
        )
        conn.commit()
        conn.close()
        return {**use, "reward_added": 0.0, "new_credit_balance": 0.0, "self_use": True}

    conn.execute(
        """UPDATE referral_uses
           SET payment_confirmed = 1, reward_triggered = 1, confirmed_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (use["id"],),
    )
    # Increment the code's use count only when payment is actually confirmed.
    conn.execute(
        "UPDATE referral_codes SET uses_count = uses_count + 1 WHERE id = ?", (use["referral_code_id"],)
    )
    conn.commit()
    conn.close()

    # Accumulate credit for the code owner (every confirmed payment adds reward)
    credits = add_credit(
        owner_email=use["owner_email"],
        owner_name=use["owner_name"],
        amount=use["reward_for_owner"],
        referral_use_id=use["id"],
        note=f"Friend {use.get('referee_name') or use['referee_email']} paid",
    )

    # Return values reflecting the update
    use["payment_confirmed"] = 1
    use["reward_triggered"] = 1
    use["confirmed_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    use["new_balance"] = credits["balance"]
    use["total_earned"] = credits["total_earned"]
    return use


def list_referral_codes() -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM referral_codes ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_referral_uses() -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """SELECT ru.*, rc.code, rc.owner_name, rc.owner_email
           FROM referral_uses ru
           JOIN referral_codes rc ON ru.referral_code_id = rc.id
           ORDER BY ru.created_at DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Referral Credits (cumulative balance)
# ---------------------------------------------------------------------------

def get_credit_balance(owner_email: str) -> dict | None:
    """Returns the credit record for this owner, or None if no credits yet."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM referral_credits WHERE owner_email = ?",
        (owner_email.lower().strip(),),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def add_credit(
    owner_email: str,
    owner_name: str,
    amount: float,
    referral_use_id: int | None = None,
    note: str | None = None,
) -> dict:
    """
    Add earned credit to owner's balance.
    Creates the credit record if it doesn't exist (UPSERT).
    Returns the updated referral_credits row.
    """
    email = owner_email.lower().strip()
    conn = get_db()
    conn.execute(
        """INSERT INTO referral_credits (owner_email, owner_name, total_earned, balance)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(owner_email) DO UPDATE SET
               total_earned = total_earned + excluded.total_earned,
               balance      = balance + excluded.balance,
               owner_name   = excluded.owner_name,
               updated_at   = CURRENT_TIMESTAMP""",
        (email, owner_name, amount, amount),
    )
    conn.execute(
        """INSERT INTO referral_credit_events
           (owner_email, event_type, amount, referral_use_id, note)
           VALUES (?, 'earned', ?, ?, ?)""",
        (email, amount, referral_use_id, note),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM referral_credits WHERE owner_email = ?", (email,)
    ).fetchone()
    conn.close()
    return dict(row)


def redeem_credit(
    owner_email: str,
    amount: float,
    booking_id: int | None = None,
    note: str | None = None,
) -> dict | None:
    """
    Deduct `amount` from owner's balance.
    Returns the updated credit record, or None if balance is insufficient.
    """
    email = owner_email.lower().strip()
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM referral_credits WHERE owner_email = ?", (email,)
    ).fetchone()
    if not row or row["balance"] < amount:
        conn.close()
        return None
    conn.execute(
        """UPDATE referral_credits
           SET balance = balance - ?, updated_at = CURRENT_TIMESTAMP
           WHERE owner_email = ?""",
        (amount, email),
    )
    conn.execute(
        """INSERT INTO referral_credit_events
           (owner_email, event_type, amount, booking_id, note)
           VALUES (?, 'redeemed', ?, ?, ?)""",
        (email, -amount, booking_id, note),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM referral_credits WHERE owner_email = ?", (email,)
    ).fetchone()
    conn.close()
    return dict(row)


def get_credit_history(owner_email: str) -> list[dict]:
    """All credit events for this owner, newest first."""
    conn = get_db()
    rows = conn.execute(
        """SELECT ce.*, ru.referee_name, ru.referee_email as friend_email
           FROM referral_credit_events ce
           LEFT JOIN referral_uses ru ON ce.referral_use_id = ru.id
           WHERE ce.owner_email = ?
           ORDER BY ce.created_at DESC""",
        (owner_email.lower().strip(),),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_credit_balances() -> list[dict]:
    """All owners with credit balances, for admin view."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM referral_credits ORDER BY balance DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
