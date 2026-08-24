"""Canonical delivery timeline and video add-on copy across public surfaces."""

from pathlib import Path
import re

import assistant_engine as assistant


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"

ORIGINALS = "All original photos are delivered within 6–7 business days"
RETOUCH = "within an additional 6–7 business days"


def _read(name):
    return (TEMPLATES / name).read_text(encoding="utf-8")


def test_active_public_surfaces_state_both_delivery_stages():
    for name in (
        "index_v2.html",
        "landing_wedding_v5.html",
        "landing_family_v2.html",
        "landing_maternity_v2.html",
        "events_landing.html",
        "package_book.html",
        "payment.html",
    ):
        html = _read(name)
        visible_copy = re.sub(r"<[^>]+>", "", html)
        assert ORIGINALS in visible_copy, name
        assert RETOUCH in visible_copy, name


def test_payment_surface_has_current_video_product_and_multilingual_timeline():
    html = _read("payment.html")

    assert "Short Vertical Highlight Video — Up to 2 Minutes" in html
    assert "up to 2 minutes" in html
    assert "$99.00" in html
    assert "bts_reel: 99" in html
    assert "delivery_title" in html
    assert "Сроки готовности фотографий" in html
    assert "Строки готовності фотографій" in html
    assert "फ़ोटो डिलीवरी का समय" in html
    assert "up to 1 minute" not in html.lower()


def test_assistant_fallback_explains_both_delivery_stages_in_english_and_russian():
    context = {"facts": {"instagram": "@pashynska.photo"}, "events": ""}

    english = assistant._fallback_answer("When will my gallery be ready?", context, "en")
    russian = assistant._fallback_answer("Когда будут готовы фото и ретушь?", context, "ru")

    assert "within 6–7 business days after your session" in english
    assert "within an additional 6–7 business days" in english
    assert "6–7 рабочих дней после съёмки" in russian
    assert "ещё в течение 6–7 рабочих дней" in russian


def test_gift_certificate_preview_uses_same_video_product():
    html = (ROOT / "gift-referral" / "certificate_preview_v2.html").read_text(encoding="utf-8")

    assert "Highlight video up to 2 minutes" in html
    assert "+$99" in html
    assert 'video:{l:"Highlight video up to 2 minutes",a:99}' in html
    assert "1-minute highlight video" not in html
