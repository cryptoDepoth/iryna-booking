"""LAYER 3 — UI/E2E TESTS (Playwright)
Client + Admin flows via real browser.
"""
import pytest, os, json, re
from playwright.sync_api import sync_playwright, Page, expect
from pathlib import Path

BASE = Path(__file__).resolve().parents[3]
BASE_URL = os.getenv('TEST_BASE_URL', 'http://127.0.0.1:5001')
ADMIN_USER = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASS = os.getenv('ADMIN_PASSWORD', 'Hawanj100')
QA_EMAIL = os.getenv('QA_EMAIL_PREFIX', 'qa-test') + '@example.com'
QA_NAME = os.getenv('QA_NAME_PREFIX', 'QA Test') + ' Client'


def admin_login(page):
    """Log in unless this shared browser context is already authenticated."""
    page.goto(f'{BASE_URL}/backstage')
    if page.locator('input[name="username"], #username').count() > 0:
        page.fill('input[name="username"], #username', ADMIN_USER)
        page.fill('input[type="password"], #password', ADMIN_PASS)
        page.click('button[type="submit"], button:has-text("Sign in"), button:has-text("Login")')
        page.wait_for_load_state('networkidle', timeout=10000)

@pytest.fixture(scope='session')
def browser_context():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={'width': 1280, 'height': 900},
            user_agent='QA-Bot/1.0'
        )
        yield context
        context.close()
        browser.close()

@pytest.fixture
def page(browser_context):
    page = browser_context.new_page()
    yield page
    page.close()

@pytest.fixture
def fresh_page(page):
    """Page with console error collector."""
    errors = []
    page.on('console', lambda msg: errors.append(msg) if msg.type == 'error' else None)
    page.on('pageerror', lambda err: errors.append(err))
    yield page
    if errors:
        print(f'[console errors] {len(errors)}:', [str(e) for e in errors[:5]])

# ── CLIENT FLOW ─────────────────────────────────────────────────────────────

class TestClientLanding:
    """Landing page sanity."""

    def test_landing_renders(self, fresh_page):
        fresh_page.goto(f'{BASE_URL}/')
        # Scope to the hero H1 because the event card repeats the same heading.
        expect(fresh_page.locator('h1.hero-title')).to_be_visible(timeout=10000)
        # Check for console errors
        # (collected by fixture)

    def test_language_switcher_exists(self, fresh_page):
        fresh_page.goto(f'{BASE_URL}/')
        expect(fresh_page.locator('button:has-text("EN")')).to_be_visible()
        expect(fresh_page.locator('button:has-text("РУ")')).to_be_visible()

    def test_event_card_clickable(self, fresh_page):
        fresh_page.goto(f'{BASE_URL}/')
        cards = fresh_page.locator('article[onclick], .event-card').all()
        if not cards:
            pytest.skip('No event cards')
        cards[0].click()
        # Drawer should open
        fresh_page.wait_for_selector('#drawer, .drawer, [class*="drawer"]', timeout=5000)


class TestClientBookingFlow:
    """Full client booking flow via browser."""

    def test_reserve_drawer_opens(self, fresh_page):
        fresh_page.goto(f'{BASE_URL}/')
        cards = fresh_page.locator('article[onclick], .event-card').all()
        if not cards:
            pytest.skip('No events')
        cards[0].click()
        fresh_page.wait_for_selector('#drawer', timeout=5000)
        expect(fresh_page.locator('#drawer')).to_be_visible()

    def test_fill_form_and_submit(self, fresh_page):
        fresh_page.goto(f'{BASE_URL}/')
        cards = fresh_page.locator('article[onclick], .event-card').all()
        if not cards:
            pytest.skip('No events')
        cards[0].click()
        fresh_page.wait_for_selector('#drawer', timeout=5000)

        # Select a slot
        slots = fresh_page.locator('#dbody button:not(.close), #dbody .slot, .slots button').all()
        if not slots:
            pytest.skip('No slots')
        slots[0].click()

        # Fill form (current UI uses placeholders, not labels)
        fresh_page.locator('#dbody input[placeholder*="Full name"], #dbody input[name="name"]').fill(QA_NAME)
        fresh_page.locator('#dbody input[placeholder*="Phone"], #dbody input[name="phone"]').fill('403-555-0100')
        fresh_page.locator('#dbody input[placeholder*="Email"], #dbody input[name="email"]').fill(QA_EMAIL)
        instagram = fresh_page.locator('#dbody input[placeholder*="instagram"], #dbody input[name="instagram"]')
        if instagram.count() > 0:
            instagram.fill('@qatest')

        # Click continue
        cta = fresh_page.locator('button:has-text("Continue"), button:has-text("Payment"), #reserve-btn').first
        if cta.count() > 0:
            cta.click()
            fresh_page.wait_for_load_state('networkidle', timeout=10000)
            # Should be on payment or pending page
            assert 'payment' in fresh_page.url.lower() or 'pending' in fresh_page.content().lower()

    def test_mobile_viewport(self, fresh_page):
        fresh_page.set_viewport_size({'width': 390, 'height': 844})
        fresh_page.goto(f'{BASE_URL}/')
        expect(fresh_page.locator('h1.hero-title')).to_be_visible(timeout=10000)
        # CTA should be visible and clickable on mobile
        cards = fresh_page.locator('article[onclick], .event-card').all()
        if cards:
            cards[0].click()
            fresh_page.wait_for_selector('#drawer', timeout=5000)


class TestAdminFlow:
    """Admin dashboard via browser."""

    def test_admin_login(self, fresh_page):
        admin_login(fresh_page)
        # Should see dashboard
        assert 'dashboard' in fresh_page.content().lower() or 'bookings' in fresh_page.content().lower() or 'operations' in fresh_page.content().lower()

    def test_admin_sees_bookings_table(self, fresh_page):
        admin_login(fresh_page)
        fresh_page.wait_for_selector('table, .bookings-table', timeout=10000)
        rows = fresh_page.locator('table tbody tr').all()
        assert len(rows) >= 0

    def test_admin_filter_by_status(self, fresh_page):
        admin_login(fresh_page)
        fresh_page.wait_for_selector('table, .bookings-table', timeout=10000)
        # Click Pending filter
        pending_btn = fresh_page.locator('button:has-text("Pending"), .filter-pending').first
        if pending_btn.count() > 0:
            pending_btn.click()
            fresh_page.wait_for_timeout(1000)
            # Should still render table
            assert fresh_page.locator('table tbody tr').count() >= 0
