"""Rendered accessibility regressions for the public booking surfaces.

These tests use a deterministic set of future mini sessions and block every
non-local browser request.  They cover the keyboard and accessibility failures
found during the July 2026 public-site audit without exercising live providers.
"""

import copy
import sqlite3
import threading
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

import app as booking_app


MISSION_EVENTS = [
    {
        "id": f"mission-mini-{index}",
        "title": f"Mission Test Mini {name}",
        "subtitle": "Isolated local validation fixture",
        "date": date,
        "start_time": "10:00",
        "end_time": "13:00",
        "session_length": 20,
        "break_length": 10,
        "slot_interval": 30,
        "deposit": 100.0,
        "full_price": 300.0,
        "location": "Calgary test location",
        "session_type": "mini",
        "featured": index == 1,
        "included": ["20 minute session", "15 edited photos"],
        "photos": [photo],
        "status": "active",
        "agreement": {
            "enabled": True,
            "require_terms": True,
            "require_marketing_choice": True,
            "terms_version": "mission-accessibility-v1",
        },
    }
    for index, name, date, photo in (
        (1, "One", "2099-08-01", "/images/family-ss-13.webp"),
        (2, "Two", "2099-08-08", "/images/family-ss-8.webp"),
        (3, "Three", "2099-08-15", "/images/family-ss-4.webp"),
        (4, "Four", "2099-08-22", "/images/boho-swing.jpeg"),
        (5, "Five", "2099-08-29", "/images/maternity-ss-2.webp"),
        (6, "Six", "2099-09-05", "/images/wedding-ss-6.webp"),
    )
]

PUBLIC_PATHS = ("/", "/family", "/maternity", "/book?type=mini")


def _chromium_launch_options(playwright):
    launch_options = {"headless": True}
    if not Path(playwright.chromium.executable_path).exists():
        candidates = sorted(
            (Path.home() / "Library" / "Caches" / "ms-playwright").glob(
                "chromium_headless_shell-*/chrome-headless-shell-mac-arm64/"
                "chrome-headless-shell"
            )
        )
        assert candidates, "A local Playwright Chromium binary is required"
        launch_options["executable_path"] = str(candidates[-1])
    return launch_options


def _seed_sold_out_event(db_path):
    event = next(event for event in MISSION_EVENTS if event["id"] == "mission-mini-6")
    conn = sqlite3.connect(db_path)
    for index, slot in enumerate(booking_app.generate_slots(event), start=1):
        conn.execute(
            """
            INSERT INTO bookings (
                date, time, name, email, phone, instagram, session_type,
                status, paid, confirmed, reserved_until, event_id,
                confirmation_token, deposit_amount, full_price
            ) VALUES (?, ?, ?, ?, ?, ?, 'mini', 'confirmed', 1, 1, ?, ?, ?, 100, 300)
            """,
            (
                event["date"],
                slot["time"],
                f"Mission Sold Out {index}",
                f"soldout{index}@example.test",
                f"40355501{index:02d}",
                "mission.soldout",
                "2099-09-06T00:00:00-06:00",
                event["id"],
                f"mission-soldout-{index}",
            ),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def accessibility_server(tmp_path, monkeypatch):
    db_path = tmp_path / "public-accessibility.db"
    monkeypatch.setattr(booking_app, "DB_PATH", str(db_path))
    monkeypatch.setattr(booking_app, "_IMAGE_CACHE_DIR", str(tmp_path / "image-cache"))
    monkeypatch.setattr(booking_app, "EVENTS", copy.deepcopy(MISSION_EVENTS))
    monkeypatch.setattr(
        booking_app,
        "SETTINGS",
        {
            "photographer_email": "test@example.com",
            "photographer_name": "Test Photographer",
            "photographer_instagram": "@test",
            "photographer_instagram_url": "https://example.com",
            "reservation_minutes": 15,
            "currency": "CAD",
            "tax_label": "+GST",
            "timezone": "America/Edmonton",
        },
    )
    monkeypatch.setattr(booking_app, "NOTION_API_KEY", "")
    monkeypatch.setattr(booking_app, "STRIPE_SECRET_KEY", "")
    monkeypatch.setattr(
        booking_app,
        "_start_etransfer_checker",
        lambda booking_id: None,
        raising=False,
    )
    monkeypatch.setattr(booking_app, "sync_to_notion", lambda booking_id: None)
    monkeypatch.setattr(booking_app, "_notify_new_reservation", lambda **kwargs: None)
    monkeypatch.setattr(booking_app, "_notify_payment_pending", lambda **kwargs: None)
    monkeypatch.setattr(booking_app, "_notify_waitlist_signup", lambda entry: None)
    booking_app._rate_limits.clear()
    booking_app._assistant_attempts.clear()
    booking_app.init_db()
    _seed_sold_out_event(db_path)

    monkeypatch.setitem(booking_app.app.config, "TESTING", True)
    server = make_server("127.0.0.1", 0, booking_app.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield {
        "base_url": f"http://127.0.0.1:{server.server_port}",
        "db_path": db_path,
    }

    server.shutdown()
    thread.join(timeout=5)
    server.server_close()
    booking_app._rate_limits.clear()
    booking_app._assistant_attempts.clear()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(**_chromium_launch_options(playwright))
        yield instance
        instance.close()


def _new_local_page(browser):
    context = browser.new_context(
        viewport={"width": 390, "height": 844},
        device_scale_factor=3,
        is_mobile=True,
        has_touch=True,
    )
    page = context.new_page()
    blocked = []

    def intercept(route):
        host = urlsplit(route.request.url).hostname
        if host not in {"127.0.0.1", "localhost"}:
            blocked.append(route.request.url)
            route.abort()
            return
        route.continue_()

    page.route("**/*", intercept)
    return context, page, blocked


def _visible_heading_tree(page):
    return page.evaluate(
        """
        () => [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')]
          .filter(element => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
              && style.visibility !== 'hidden'
              && rect.width > 0
              && rect.height > 0;
          })
          .map(element => ({
            level: Number(element.tagName.slice(1)),
            text: element.innerText.trim().replace(/\\s+/g, ' '),
          }))
        """
    )


def _tab_focus_states(page, count):
    states = []
    for _ in range(count):
        page.keyboard.press("Tab")
        states.append(
            page.evaluate(
                """
                () => {
                  const element = document.activeElement;
                  const style = getComputedStyle(element);
                  return {
                    tag: element.tagName,
                    text: (
                      element.innerText
                      || element.getAttribute('aria-label')
                      || ''
                    ).trim().replace(/\\s+/g, ' ').slice(0, 80),
                    outlineStyle: style.outlineStyle,
                    outlineWidth: parseFloat(style.outlineWidth) || 0,
                  };
                }
                """
            )
        )
    return states


def _contrast_ratio(page, selector, background="#ffffff", property_name="color"):
    return page.locator(selector).first.evaluate(
        """
        (element, options) => {
          const parse = value => {
            const input = String(value).trim();
            if (input.startsWith('#')) {
              const hex = input.slice(1);
              const normalized = hex.length === 3
                ? [...hex].map(character => character + character).join('')
                : hex.slice(0, 6);
              return [0, 2, 4].map(index => parseInt(normalized.slice(index, index + 2), 16));
            }
            const channels = input.match(/[\\d.]+/g);
            if (!channels || channels.length < 3) {
              throw new Error(`Unsupported CSS color: ${input}`);
            }
            return channels.slice(0, 3).map(Number);
          };
          const luminance = value => {
            const channels = parse(value).map(channel => {
              channel /= 255;
              return channel <= 0.04045
                ? channel / 12.92
                : Math.pow((channel + 0.055) / 1.055, 2.4);
            });
            return (
              0.2126 * channels[0]
              + 0.7152 * channels[1]
              + 0.0722 * channels[2]
            );
          };
          const foregroundLuminance = luminance(
            getComputedStyle(element)[options.propertyName]
          );
          const backgroundLuminance = luminance(options.background);
          const lighter = Math.max(foregroundLuminance, backgroundLuminance);
          const darker = Math.min(foregroundLuminance, backgroundLuminance);
          return (lighter + 0.05) / (darker + 0.05);
        }
        """,
        {"background": background, "propertyName": property_name},
    )


def _tab_to(page, selector, limit=80):
    for _ in range(limit):
        page.keyboard.press("Tab")
        if page.locator(selector).evaluate("element => element === document.activeElement"):
            return
    pytest.fail(f"Keyboard focus never reached {selector}")


def test_named_pages_have_coherent_headings_image_alternatives_and_focus(
    accessibility_server,
    browser,
):
    base_url = accessibility_server["base_url"]
    for path in PUBLIC_PATHS:
        context, page, blocked = _new_local_page(browser)
        page.goto(base_url + path, wait_until="domcontentloaded")
        page.wait_for_timeout(500)

        headings = _visible_heading_tree(page)
        assert sum(heading["level"] == 1 for heading in headings) == 1
        assert all(
            current["level"] <= previous["level"] + 1
            for previous, current in zip(headings, headings[1:])
        ), (path, headings)

        images = page.locator("img").all()
        for image in images:
            alt = image.get_attribute("alt")
            hidden = image.get_attribute("aria-hidden") == "true"
            presentation = image.get_attribute("role") in {"none", "presentation"}
            assert alt is not None or hidden or presentation
            if alt not in {None, ""}:
                assert alt.strip()

        focus_states = _tab_focus_states(page, 8)
        assert all(state["tag"] != "BODY" for state in focus_states), (path, focus_states)
        assert all(
            state["outlineStyle"] != "none" and state["outlineWidth"] >= 3
            for state in focus_states
        ), (path, focus_states)

        assert all(urlsplit(url).hostname not in {"127.0.0.1", "localhost"} for url in blocked)
        context.close()


@pytest.mark.parametrize(
    ("path", "selectors"),
    (
        (
            "/",
            (
                ".nav .brand",
                ".nav-gift",
                ".cat-pills .cp",
                ".hero-cta",
                ".filters .chip",
                ".event-card .price-bar .cta",
            ),
        ),
        ("/family", (".m-nav .logo", ".m-nav .ghost", ".m-nav .gift")),
        ("/maternity", (".m-nav .logo", ".m-nav .ghost", ".m-nav .gift")),
        (
            "/book?type=mini",
            (
                ".hero-jump",
                ".hero-whatsapp",
                ".filters .chip",
                ".featured",
                ".event-card",
            ),
        ),
    ),
)
def test_primary_mobile_controls_have_usable_targets(
    accessibility_server,
    browser,
    path,
    selectors,
):
    context, page, _blocked = _new_local_page(browser)
    page.goto(accessibility_server["base_url"] + path, wait_until="domcontentloaded")
    page.wait_for_timeout(500)

    for selector in selectors:
        target = page.locator(selector).first
        box = target.bounding_box()
        assert box is not None, (path, selector)
        assert box["height"] >= 44, (path, selector, box)
        assert box["width"] >= 44, (path, selector, box)

    context.close()


def test_representative_mobile_text_and_control_colors_meet_aa(
    accessibility_server,
    browser,
):
    base_url = accessibility_server["base_url"]
    checks = (
        ("/", ".hero-pill.rose", "#8f6a24"),
        ("/", ".event-card .type-tag", "#ffffff"),
        ("/family", ".m-nav .ghost", "#ffffff"),
        ("/family", ".footer-copy", "#fbf7f0"),
        ("/maternity", ".m-nav .ghost", "#ffffff"),
        ("/maternity", ".footer-copy", "#fbf7f0"),
        ("/book?type=mini", ".event-card .type-tag", "#ffffff"),
        ("/book?type=mini", "footer", "#faf7f5"),
    )
    for path, selector, background in checks:
        context, page, _blocked = _new_local_page(browser)
        page.goto(base_url + path, wait_until="domcontentloaded")
        page.wait_for_timeout(300)
        assert _contrast_ratio(page, selector, background) >= 4.5, (path, selector)
        context.close()


def test_booking_and_waitlist_drawer_semantics_errors_and_focus_lifecycle(
    accessibility_server,
    browser,
):
    context, page, _blocked = _new_local_page(browser)
    page.goto(accessibility_server["base_url"] + "/", wait_until="domcontentloaded")
    page.wait_for_timeout(500)

    inventory = page.evaluate("fetch('/events').then(response => response.json())")
    events_by_id = {event["id"]: event for event in inventory["events"]}
    assert events_by_id["mission-mini-1"]["spots_left"] > 0
    assert events_by_id["mission-mini-6"]["spots_left"] == 0

    opener = page.locator(".ssr-hero .hero-cta").first
    opener.focus()
    opener.press("Enter")
    dialog = page.get_by_role("dialog", name="Mission Test Mini One")
    dialog.wait_for(state="visible")
    assert page.locator("#drawer .close").evaluate(
        "element => element === document.activeElement"
    )

    page.keyboard.press("Shift+Tab")
    assert page.locator("#drawer").evaluate(
        "drawer => drawer.contains(document.activeElement)"
    )
    page.keyboard.press("Tab")
    assert page.locator("#drawer .close").evaluate(
        "element => element === document.activeElement"
    )

    for label in (
        "Full name",
        "Phone",
        "Email",
        "Instagram",
        "Referral or gift code",
    ):
        assert page.get_by_label(label, exact=False).count() == 1

    slots = dialog.get_by_role("button", name="10:00")
    assert slots.get_attribute("aria-pressed") == "true"
    dialog.get_by_role("button", name="10:30").press("Enter")
    assert slots.get_attribute("aria-pressed") == "false"
    assert dialog.get_by_role("button", name="10:30").get_attribute("aria-pressed") == "true"

    page.locator("#reserve-btn").press("Enter")
    for field_id in ("fName", "fEmail"):
        field = page.locator(f"#{field_id}")
        error_id = field.get_attribute("aria-describedby")
        assert field.get_attribute("aria-invalid") == "true"
        assert error_id
        assert page.locator(f"#{error_id}").get_attribute("role") == "alert"
    assert _contrast_ratio(page, "#fName-error", "#ffffff") >= 4.5
    assert _contrast_ratio(page, "#fName", "#ffffff", "borderTopColor") >= 3

    page.locator("#fName").fill("Agreement Client")
    page.locator("#fPhone").fill("4035550198")
    page.locator("#fEmail").fill("agreement@example.test")
    page.locator("#reserve-btn").press("Enter")
    terms_group = page.locator("#termsAgreement")
    assert terms_group.get_attribute("aria-invalid") == "true"
    assert terms_group.get_attribute("aria-describedby") == "termsAgreement-error"
    assert page.locator("#termsAgreement-error").get_attribute("role") == "alert"
    assert page.locator("#termsAccepted").evaluate(
        "element => element === document.activeElement"
    )

    page.locator("#termsAccepted").press("Space")
    page.locator("#reserve-btn").press("Enter")
    assert page.locator("#agreementName").get_attribute("aria-invalid") == "true"
    assert page.locator("#agreementName").evaluate(
        "element => element === document.activeElement"
    )

    page.locator("#agreementName").fill("Agreement Client")
    page.locator("#reserve-btn").press("Enter")
    privacy_group = page.locator("#privacyAgreement")
    assert privacy_group.get_attribute("aria-invalid") == "true"
    assert privacy_group.get_attribute("aria-describedby") == "privacyAgreement-error"
    assert page.locator("#privacyAgreement-error").get_attribute("role") == "alert"
    assert page.locator('input[name="marketingConsent"]').first.evaluate(
        "element => element === document.activeElement"
    )

    page.keyboard.press("Escape")
    assert not page.locator("#drawer").evaluate("drawer => drawer.classList.contains('open')")
    assert opener.evaluate("element => element === document.activeElement")

    waitlist_opener = page.locator(
        'article[data-event-id="mission-mini-6"] .cta'
    ).first
    waitlist_opener.focus()
    waitlist_opener.press("Enter")
    waitlist_dialog = page.get_by_role("dialog", name="Mission Test Mini Six")
    waitlist_dialog.wait_for(state="visible")
    for label in ("Full name", "Email", "Instagram"):
        assert waitlist_dialog.get_by_label(label, exact=False).count() == 1

    waitlist_dialog.get_by_role("button", name="Join waitlist").press("Enter")
    for field_id in ("wName", "wEmail"):
        field = page.locator(f"#{field_id}")
        error_id = field.get_attribute("aria-describedby")
        assert field.get_attribute("aria-invalid") == "true"
        assert error_id
        assert page.locator(f"#{error_id}").get_attribute("role") == "alert"

    page.keyboard.press("Escape")
    assert waitlist_opener.evaluate("element => element === document.activeElement")
    context.close()


@pytest.mark.parametrize("path", ("/family", "/maternity", "/book?type=mini"))
def test_public_accordions_are_keyboard_operable(accessibility_server, browser, path):
    context, page, _blocked = _new_local_page(browser)
    page.goto(accessibility_server["base_url"] + path, wait_until="domcontentloaded")
    summary = page.locator("details summary").first
    summary.scroll_into_view_if_needed()
    summary.focus()
    summary.press("Enter")
    assert summary.evaluate("element => element.parentElement.open")
    summary.press("Enter")
    assert not summary.evaluate("element => element.parentElement.open")
    context.close()


def test_testimonial_carousel_arrow_navigation_keeps_focus_exposed(
    accessibility_server,
    browser,
):
    context, page, _blocked = _new_local_page(browser)
    page.goto(accessibility_server["base_url"] + "/", wait_until="domcontentloaded")
    page.wait_for_timeout(500)

    marquee = page.locator("#tmtMarquee")
    marquee.focus()
    active_before = page.locator(".tmt-card.is-active").evaluate(
        "element => [...element.parentElement.children].indexOf(element)"
    )
    marquee.press("ArrowRight")
    page.wait_for_timeout(500)
    active_after = page.locator(".tmt-card.is-active").evaluate(
        "element => [...element.parentElement.children].indexOf(element)"
    )
    assert active_after > active_before
    assert marquee.evaluate("element => element === document.activeElement")

    read_more = page.locator(".tmt-card.is-active .tmt-more")
    if read_more.count():
        read_more.focus()
        focused_card = read_more.locator("xpath=ancestor::article").first
        read_more.press("ArrowRight")
        page.wait_for_timeout(350)
        assert read_more.evaluate("element => element === document.activeElement")
        assert focused_card.get_attribute("aria-hidden") == "false"

    context.close()


def test_assistant_input_has_a_programmatic_name(accessibility_server, browser):
    context, page, _blocked = _new_local_page(browser)
    page.goto(accessibility_server["base_url"] + "/", wait_until="domcontentloaded")
    page.get_by_role("button", name="Open assistant").click()
    assert page.get_by_label("Ask a question").count() == 1
    context.close()


def test_keyboard_booking_reaches_payment_step_and_tokenized_route_without_providers(
    accessibility_server,
    browser,
):
    base_url = accessibility_server["base_url"]
    context, page, blocked = _new_local_page(browser)
    page.goto(base_url + "/book?type=mini", wait_until="domcontentloaded")
    page.wait_for_timeout(400)

    _tab_to(page, 'a.event-card[data-event="mission-mini-1"]')
    page.keyboard.press("Enter")
    page.wait_for_url("**/?event=mission-mini-1")
    dialog = page.get_by_role("dialog", name="Mission Test Mini One")
    dialog.wait_for(state="visible")

    page.locator("#drawer .close").press("Tab")
    _tab_to(page, "#fName")
    page.keyboard.type("Keyboard Client")
    _tab_to(page, "#fPhone")
    page.keyboard.type("4035550199")
    _tab_to(page, "#fEmail")
    page.keyboard.type("keyboard@example.test")
    _tab_to(page, "#termsAccepted")
    page.keyboard.press("Space")
    _tab_to(page, "#agreementName")
    page.keyboard.type("Keyboard Client")
    _tab_to(page, 'input[name="marketingConsent"][value="yes"]')
    page.keyboard.press("ArrowDown")
    assert page.locator('input[name="marketingConsent"][value="no"]').is_checked()
    _tab_to(page, "#reserve-btn")
    page.keyboard.press("Enter")

    page.locator("#paymentStepTitle").wait_for(state="visible")
    assert page.locator("#paymentStepTitle").evaluate(
        "element => element === document.activeElement"
    )
    assert dialog.get_by_text("Amount due today").is_visible()

    booking = page.evaluate("currentBooking")
    assert booking["booking_id"]
    assert booking["confirmation_token"]
    payment_url = (
        f"{base_url}/payment?booking_id={booking['booking_id']}"
        f"&token={booking['confirmation_token']}"
    )
    response = page.goto(payment_url, wait_until="domcontentloaded")
    assert response.status == 200
    assert page.get_by_text("Complete Your Booking", exact=True).is_visible()

    conn = sqlite3.connect(accessibility_server["db_path"])
    row = conn.execute(
        """
        SELECT event_id, date, time, name, email, status, confirmation_token
        FROM bookings WHERE id=?
        """,
        (booking["booking_id"],),
    ).fetchone()
    conn.close()
    assert row == (
        "mission-mini-1",
        "2099-08-01",
        "10:00",
        "Keyboard Client",
        "keyboard@example.test",
        "reserved",
        booking["confirmation_token"],
    )

    assert blocked
    assert all(
        urlsplit(url).hostname not in {"127.0.0.1", "localhost"}
        for url in blocked
    )
    context.close()
