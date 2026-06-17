"""
Gift certificate security utilities.
Rate limiting, input validation, honeypot checks, email-safe output.
"""
import re
import time
from collections import defaultdict
from html import escape as _html_escape

# ---------------------------------------------------------------------------
# Rate Limiter (in-memory, per IP)
# ---------------------------------------------------------------------------

_gift_rate_limit: dict[str, list[float]] = defaultdict(list)


def check_rate_limit(ip: str, max_requests: int = 3, window_seconds: int = 3600) -> bool:
    """Returns True if the request is allowed, False if rate-limited."""
    now = time.time()
    _gift_rate_limit[ip] = [t for t in _gift_rate_limit[ip] if now - t < window_seconds]
    if len(_gift_rate_limit[ip]) >= max_requests:
        return False
    _gift_rate_limit[ip].append(now)
    return True


def _reset_rate_limit(ip: str) -> None:
    """Test helper: clear rate-limit state for an IP."""
    _gift_rate_limit.pop(ip, None)


# ---------------------------------------------------------------------------
# Spam patterns
# ---------------------------------------------------------------------------

_URL_RE = re.compile(
    r"https?://"
    r"|www\."
    r"|\.com\b|\.net\b|\.org\b|\.io\b|\.ly\b|\.ph\b|\.gg\b"
    r"|telegra\.ph|t\.me|bit\.ly|tinyurl"
    r"|discord\.gg|telegram\b|whatsapp\b",
    re.IGNORECASE,
)

_SPAM_KEYWORDS = [
    "crypto", "bitcoin", "binance", "investment", "profit",
    "earn money", "click here", "free money", "make money",
    "wire transfer", "western union", "moneygram", "gift card",
    "telegram", "whatsapp", "discord", "onlyfans",
]

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

_SUSPICIOUS_DOMAINS = {
    "tempmail", "throwaway", "guerrilla", "mailinator",
    "yopmail", "sharklasers", "guerrillamail", "spam4",
    "trashmail", "dispostable", "fakeinbox", "maildrop",
    "getnada", "mohmal", "10minute", "minutemail",
}


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

def validate_name_field(value: str, field_name: str = "Name") -> tuple[bool, str]:
    """Validate a name field (purchaser_name, recipient_name, etc.)."""
    value = (value or "").strip()
    if not value:
        return False, f"{field_name} is required"
    if len(value) > 100:
        return False, f"{field_name} is too long (max 100 characters)"
    if _URL_RE.search(value):
        return False, f"{field_name} cannot contain URLs or links"
    if "@" in value:
        return False, f"{field_name} should not contain email addresses"
    for kw in _SPAM_KEYWORDS:
        if kw.lower() in value.lower():
            return False, f"{field_name} contains prohibited content"
    return True, ""


def validate_email_field(value: str, field_name: str = "Email") -> tuple[bool, str]:
    """Validate a required email address field."""
    value = (value or "").strip().lower()
    if not value:
        return False, f"{field_name} is required"
    if len(value) > 200:
        return False, f"{field_name} is too long"
    if not _EMAIL_RE.match(value):
        return False, f"{field_name} is not a valid email address"
    domain = value.split("@")[-1]
    for sus in _SUSPICIOUS_DOMAINS:
        if sus in domain:
            return False, f"{field_name} appears to be a disposable/temporary address"
    return True, ""


def validate_optional_email(value: str, field_name: str = "Email") -> tuple[bool, str]:
    """Validate an optional email field — empty is OK."""
    if not (value or "").strip():
        return True, ""
    return validate_email_field(value, field_name)


def validate_message_field(value: str, field_name: str = "Message") -> tuple[bool, str]:
    """Validate a personal message / freetext field."""
    value = (value or "").strip()
    if len(value) > 500:
        return False, f"{field_name} is too long (max 500 characters)"
    if _URL_RE.search(value):
        return False, f"{field_name} cannot contain URLs or links"
    return True, ""


# ---------------------------------------------------------------------------
# Honeypot
# ---------------------------------------------------------------------------

def check_honeypot(form_data) -> bool:
    """
    Returns True (safe) when the honeypot field is empty.
    Bots fill every visible field; humans leave hidden fields blank.
    The hidden field must be named 'website' in the HTML form.
    """
    return not bool((form_data.get("website") or "").strip())


# ---------------------------------------------------------------------------
# Safe output helper
# ---------------------------------------------------------------------------

def safe_text(value: str) -> str:
    """HTML-escape a user-supplied string for safe embedding in HTML emails."""
    return _html_escape(str(value or ""), quote=False)
