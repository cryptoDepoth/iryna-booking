"""Regression coverage for client-facing translation completeness.

These tests intentionally focus on visible copy that previously stayed English after
switching languages, plus grammar-sensitive phrases flagged during i18n QA.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_template(name: str) -> str:
    return (ROOT / "templates" / name).read_text(encoding="utf-8")


def test_index_featured_hero_translation_keys_are_defined_and_used():
    html = read_template("index_v2.html")

    required_keys = [
        "hero_kicker_label", "hf_date", "hf_duration", "hf_deposit", "hf_currency",
        "hf_total", "hf_total_sub", "hf_spots", "hf_status", "hf_location_prefix",
        "spots_left", "see_all_btn", "sessions_heading", "sessions_subhead",
        "how_heading", "how_subhead", "session_type_fallback", "stripe_redirecting",
        "privacy_link",
    ]
    for key in required_keys:
        assert f"{key}:" in html, f"SITE_I18N missing {key}"

    assert "${left} left" not in html
    assert "CAD · e-Transfer</small>" not in html
    assert "len} min session" not in html
    assert "'@ '+loc" not in html
    assert "txt.textContent = T('stripe_redirecting')" in html


def test_payment_page_visible_copy_is_i18n_wrapped():
    html = read_template("payment.html")

    required_keys = [
        "summary_session", "summary_session_value", "summary_date", "summary_time",
        "summary_includes", "summary_includes_value", "summary_balance", "summary_deposit",
        "summary_selected_addons", "summary_addons_total",
        "currency_label", "bank_msg_title", "bank_msg_desc", "bank_msg_deposit_for",
        "how_step1", "how_step2", "how_step3", "how_step4", "how_step5", "how_step6",
        "footer_questions", "privacy_link", "reservation_expired", "copied", "copy",
        "stripe_redirecting", "stripe_error", "network_error", "processing",
    ]
    for key in required_keys:
        assert f"{key}:" in html, f"PAY_I18N missing {key}"

    assert 'class="key">Session</span>' not in html
    assert 'data-i18n="bank_msg_title"' in html
    assert 'data-i18n="bank_msg_desc"' in html
    assert 'data-i18n="how_step1"' in html
    assert 'data-i18n="summary_selected_addons"' in html
    assert 'data-i18n="summary_addons_total"' in html
    assert "No password needed! This is an auto-deposit enabled email" not in html
    assert "Questions? <a" not in html
    assert 'data-i18n="privacy_link"' in html
    assert "pill.textContent = '✓ Copied'" not in html
    assert "btn.textContent = 'Processing…'" not in html


def test_success_page_footer_and_labels_translate():
    html = read_template("success.html")

    for key in ["instagram_label", "payment_method", "studio_footer", "privacy_link"]:
        assert f"{key}:" in html, f"TRANSLATIONS missing {key}"

    assert '<span class="key">Instagram</span>' not in html
    assert '<span class="val">Interac e-Transfer</span>' not in html
    assert 'data-i18n="privacy_link"' in html
    assert "Бонус порада" not in html
    assert "Напишіть мені'," not in html


def test_privacy_page_has_language_switcher_and_all_languages():
    html = read_template("privacy.html")

    assert "PRIVACY_I18N" in html
    for lang in ["en", "ru", "hi", "uk"]:
        assert f"setPrivacyLang('{lang}')" in html
    for key in ["page_title", "notice", "storage_title", "payments_title", "questions_title"]:
        assert f"data-i18n=\"{key}\"" in html
    assert "No cookie banner" in html  # English source remains present
    assert "Баннер cookies не нужен" in html
    assert "Банер cookies не потрібен" in html
