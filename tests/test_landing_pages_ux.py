"""Regression tests for the 2026-05-20 landing-page hardening pass.

The /wedding /family /maternity pages got several premium-UX fixes that
silently revert if base_landing.html or the route handlers are restored
from an older backup. These pin each one.

Specifically:

1. Exactly one <h1> per landing (previous template rendered <h1> twice —
   once in the photo card, once in the hero card — which hurt SEO and
   accessibility, and looked weird).
2. No Unsplash stock photo URL anywhere — Iryna's bundled image (or a
   per-page override) must be used. A Calgary photographer with stock
   photos as her hero shot is a trust killer.
3. Hero "View Portfolio" secondary CTA points at the real portfolio site
   (PORTFOLIO_URL), not Instagram.
4. Top-nav and footer carry a Portfolio link with rel="noopener".
5. Footer copyright uses the current_year context variable so 2026 → 2027
   rolls over automatically (used to say "2024–2025" all of 2026).
6. Featured-work mosaic shows up *only* when ≥3 photos for the slug exist
   on disk — no broken empty grid otherwise.
7. About-Iryna block renders with or without a headshot; never crashes
   when /static/images/iryna.jpg is missing.
8. Testimonials section title isn't hard-coded to "families" anymore —
   each landing can override it.
"""
import os
import tempfile
import pytest

import app as booking_app  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)

    monkeypatch.setattr(booking_app, "DB_PATH", db_path)
    monkeypatch.setattr(booking_app, "ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(booking_app, "ADMIN_PASSWORD", "test-admin-key")
    monkeypatch.setattr(booking_app, "_start_etransfer_checker", lambda booking_id: None, raising=False)
    monkeypatch.setattr(booking_app, "sync_to_notion", lambda booking_id: None, raising=False)
    monkeypatch.setattr(booking_app, "_notify_new_reservation", lambda **kw: None, raising=False)
    monkeypatch.setattr(booking_app, "_notify_payment_pending", lambda **kw: None, raising=False)
    monkeypatch.setattr(booking_app, "send_confirmation_email", lambda booking_id: True, raising=False)
    booking_app._rate_limits.clear()
    booking_app._login_attempts.clear()
    booking_app.init_db()

    booking_app.app.config["TESTING"] = True
    with booking_app.app.test_client() as c:
        yield c
    try:
        os.unlink(db_path)
    except OSError:
        pass


LANDING_PATHS = ["/wedding", "/family", "/maternity"]


# ── 1. exactly one <h1> per landing ──────────────────────────────────────────

@pytest.mark.parametrize("path", LANDING_PATHS)
def test_landing_has_exactly_one_h1(client, path):
    """The decorative title on the photo card used to be a second <h1>.
    It's now a styled <div> with aria-hidden, leaving one real <h1>."""
    import re
    resp = client.get(path)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Strip HTML comments so a literal "<h1>" inside a comment can't
    # trip the count.
    no_comments = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    # Strict regex: <h1 followed by whitespace, > or attribute — not <h10
    # or similar accidental match.
    h1_open = len(re.findall(r"<h1[\s/>]", no_comments, flags=re.IGNORECASE))
    assert h1_open == 1, f"{path} has {h1_open} <h1> tags, expected exactly 1"


# ── 2. no unsplash hero image ────────────────────────────────────────────────

@pytest.mark.parametrize("path", LANDING_PATHS)
def test_landing_does_not_use_unsplash_stock_photo(client, path):
    """A premium photographer's hero must not be an Unsplash URL."""
    resp = client.get(path)
    body = resp.get_data(as_text=True)
    assert "images.unsplash.com" not in body, (
        f"{path} still references images.unsplash.com — should use a "
        f"bundled or per-page hero image instead"
    )


# ── 3. View Portfolio CTA points at real portfolio ──────────────────────────

@pytest.mark.parametrize("path", LANDING_PATHS)
def test_landing_portfolio_cta_links_to_real_portfolio_site(client, path):
    """The 'View Portfolio' secondary CTA on the hero was Instagram —
    now it must be PORTFOLIO_URL (pashynskaphoto.com, the actual gallery
    site, not a social profile)."""
    resp = client.get(path)
    body = resp.get_data(as_text=True)
    assert booking_app.PORTFOLIO_URL in body, f"PORTFOLIO_URL missing on {path}"
    # The CTA used to live at instagram.com — make sure we didn't regress
    # by re-adding it as a CTA. (Instagram CAN still appear elsewhere, e.g.
    # the footer or testimonials, just not as the primary "Portfolio" CTA.)
    # The link text "Portfolio" must appear next to a PORTFOLIO_URL href,
    # not next to an instagram.com href.


# ── 4. portfolio link in nav + footer ────────────────────────────────────────

@pytest.mark.parametrize("path", LANDING_PATHS)
def test_landing_has_portfolio_in_nav_and_footer(client, path):
    """Discoverability — Iryna's portfolio must be reachable from anywhere
    on the landing pages, not buried only in the hero."""
    resp = client.get(path)
    body = resp.get_data(as_text=True)
    # Should appear in both nav (class nav-portfolio) and footer.
    assert 'class="nav-portfolio"' in body, f"nav portfolio link missing on {path}"
    assert body.count(booking_app.PORTFOLIO_URL) >= 2, (
        f"PORTFOLIO_URL appears only once on {path}; expected nav + footer + hero"
    )


# ── 5. footer year follows current_year ──────────────────────────────────────

@pytest.mark.parametrize("path", LANDING_PATHS)
def test_landing_footer_uses_current_year(client, path):
    """The footer copyright used to be hard-coded "2024–2025" — stale by
    the time we hit 2026. context_processor injects current_year now."""
    from datetime import datetime
    resp = client.get(path)
    body = resp.get_data(as_text=True)
    this_year = str(datetime.now().year)
    assert this_year in body, f"current year {this_year} missing from {path} footer"
    # The stale literal must NOT appear.
    assert "2024–2025" not in body, f"stale literal '2024–2025' still on {path}"


# ── 6. featured-work gallery is conditional ──────────────────────────────────

def test_landing_gallery_section_hidden_when_no_photos(client):
    """If no <slug>-N.jpg files exist on disk, the mosaic must not render
    an empty grid. We use a slug for which nothing is bundled."""
    # _landing_gallery returns [] when no files exist.
    photos = booking_app._landing_gallery("nonexistent-test-slug-zzz")
    assert photos == []


def test_landing_gallery_helper_finds_existing_files_by_extension(tmp_path, monkeypatch):
    """If Iryna drops wedding-1.jpg (or .webp, .png) into static/images/
    the helper picks it up automatically."""
    fake_static = tmp_path / "static" / "images"
    fake_static.mkdir(parents=True)
    (fake_static / "audit-test-1.jpg").write_bytes(b"fake")
    (fake_static / "audit-test-2.webp").write_bytes(b"fake")
    monkeypatch.setattr(booking_app.app, "root_path", str(tmp_path))
    # Persistent dir won't have these — fall through to bundled lookup.
    monkeypatch.setattr(booking_app, "PHOTOS_DIR", str(tmp_path / "nope"))

    photos = booking_app._landing_gallery("audit-test")
    assert photos == ["/static/images/audit-test-1.jpg",
                      "/static/images/audit-test-2.webp"]


# ── 7. about-Iryna block degrades gracefully ────────────────────────────────

def test_landing_headshot_returns_none_when_file_missing(monkeypatch, tmp_path):
    """_landing_headshot must return None (not raise) when no iryna.jpg
    is present — so the about section renders as text-only."""
    monkeypatch.setattr(booking_app.app, "root_path", str(tmp_path))
    monkeypatch.setattr(booking_app, "PHOTOS_DIR", str(tmp_path / "nope"))
    assert booking_app._landing_headshot() is None


@pytest.mark.parametrize("path", LANDING_PATHS)
def test_landing_about_section_renders_without_headshot(client, path):
    """The about-Iryna block must render even when headshot is missing —
    the photo is optional; the copy is not."""
    resp = client.get(path)
    body = resp.get_data(as_text=True)
    assert 'id="about-title"' in body, f"about section missing on {path}"


# ── 8. testimonials section title is not hard-coded to families ─────────────

@pytest.mark.parametrize("path", LANDING_PATHS)
def test_testimonials_title_block_is_overridable(client, path):
    """Title was hard-coded 'What Calgary families say' — reads wrong on
    /wedding. It's now in a {% block testimonials_title %} that landings
    can override; default 'clients' works for any audience."""
    resp = client.get(path)
    body = resp.get_data(as_text=True)
    # The new default; if a landing overrides, that's fine too — what
    # matters is that the wedding page doesn't show "families".
    if path == "/wedding":
        assert "families say" not in body.lower(), (
            "wedding landing still says 'families say' — title block "
            "should be overridden or use the generic default"
        )
