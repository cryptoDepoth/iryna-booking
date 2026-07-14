"""Frontend/backend contract regression tests for the v2 drawer UI."""
from pathlib import Path


TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "index_v2.html"


def test_drawer_requests_slots_by_date_and_event_id():
    """The drawer must call /slots/<date>?event_id=<id>.

    Regression: API.slots used to accept one argument named eventId but callers
    passed e.date. That generated /slots/2026-06-07?event_id=2026-06-07,
    so the backend could not resolve the event and the UI showed
    "No slots available" even while /events and /slots had inventory.
    """
    html = TEMPLATE.read_text()

    assert "slots: async (date, eventId)" in html
    assert "API.slots(" in html
    assert ", e.id)" in html
    assert "?event_id=${encodeURIComponent(eventId)}" in html

    # Do not reintroduce the old one-argument contract.
    assert "slots: async (eventId)" not in html
    assert "API.slots(e.date);" not in html
    assert "fetch(`/slots/${eventId}?event_id=${eventId}`)" not in html


def test_drawer_uses_available_slots_returned_by_api_without_fake_status_filter():
    """Backend already returns only available slots; slot objects have no status field."""
    html = TEMPLATE.read_text()

    assert "const availableSlots = slotsData.slots || [];" in html
    assert "status === 'available'" not in html
    assert 'status === "available"' not in html


def test_public_assistant_widget_posts_to_assistant_chat_endpoint():
    """The public assistant should remain wired to the Flask assistant endpoint."""
    html = TEMPLATE.read_text()

    assert 'id="assistantWidget"' in html
    assert "fetch('/assistant/chat'" in html
    assert "updateAssistantLang" in html


def test_drawer_contains_addons_agreement_and_amount_summary_hooks():
    """Session-inspired upgrades must remain wired through the current drawer."""
    html = TEMPLATE.read_text()

    assert "renderAddonsSection" in html
    assert "getSelectedAddons" in html
    assert "updateBookingSummary" in html
    assert "booking-addon" in html
    assert "marketing_consent" in html
    assert "terms_accepted" in html
    assert "agreement_name" in html
    assert "Amount due today" in html
    assert "Remaining balance" in html
    assert "Change time" in html
    assert "questionnaire" not in html.lower() or "after_confirmed_payment" in html


def test_meta_campaign_traffic_uses_photo_first_hero_not_extra_text_cue():
    """Meta ad traffic should route the photo hero to the matching session without adding a duplicate text-only cue."""
    html = TEMPLATE.read_text()

    assert 'id="campaignCue"' not in html
    assert "renderCampaignLandingCue" not in html
    assert "openCampaignCueSession" not in html
    assert "campaign_cue_view" not in html
    assert "campaign_cue_cta_click" not in html
    assert "getCampaignLandingCue" in html
    assert "pickHeroEvent" in html
    assert "utm_campaign" in html
    assert "fbclid" in html
    assert "openDrawer('${featured.id}')" in html
    assert "What’s included" in html
    assert "renderIncludedSection" in html


def test_mountain_campaign_does_not_match_generic_mini_copy_or_sold_out_events():
    """A Mountain Meta click should route to a bookable mountain/outdoor event, not any generic mini."""
    html = TEMPLATE.read_text()

    assert "function pickHeroEvent()" in html
    assert "visibleEvents().filter(isBookableEvent)" in html
    assert "Number(e.spots_left || 0) > 0" in html
    assert "mountain|mountains|quarry|bragg|kananaskis|outdoor" in html
    assert "mountain|mountains|mini|bragg|kananaskis|outdoor" not in html
    assert "const featured = pickHeroEvent();" in html


def test_grid_cards_lazy_load_photo_slideshow_slides():
    """Grid cards should show multi-photo sessions without eagerly loading every slide on mobile."""
    html = TEMPLATE.read_text()

    assert "GRID_PHOTO_SLIDE_LIMIT = 3" in html
    assert "eventPhotoList(e, GRID_PHOTO_SLIDE_LIMIT)" in html
    assert "const eagerFirstPhoto = i < 3" in html
    assert 'class="card-kb kb-host"' in html
    assert 'data-photos="${escapeAttr(JSON.stringify(lazyPhotos))}"' in html
    assert 'data-bg="${escapeAttr(firstPhoto)}"' in html
    assert "hydrateCardSlides" in html
    assert "IntersectionObserver" in html
    assert "startGridPhotoSlideshows();" in html


def test_admin_photo_upload_accepts_batch_not_only_first_file():
    admin_html = (TEMPLATE.parents[0] / "admin.html").read_text()

    assert "upload-batch" in admin_html
    assert "multiple style=\"display:none\"" in admin_html
    assert "Upload up to 5 photos at a time" in admin_html
    assert "files.forEach(file => fd.append('photos', file));" in admin_html
    assert "const file = input.files[0];" not in admin_html


def test_public_funnel_formats_money_and_offers_whatsapp_help():
    """Customer-facing prices should always show cents and questions need a low-friction path."""
    html = TEMPLATE.read_text()

    assert "'%.2f'|format(hero_event.deposit" in html
    assert "$${money(deposit)}" in html
    assert "$${money(total)}" in html
    assert "$${money(e.deposit || e.price || 0)}" in html
    assert "total: money(totalPrice)" in html
    assert "total: money(e.full_price || 0)" in html
    assert "https://wa.me/13689977903" in html
    assert "whatsapp_click" in html
    assert '<main id="main-content">' in html
    assert "window.__GOOGLE_ANALYTICS_ID" in html
    assert "gtag('config', window.__GOOGLE_ANALYTICS_ID)" in html


def test_public_funnel_server_renders_current_inventory_for_crawlers():
    """The initial event cards must exist in HTML before client-side JavaScript runs."""
    html = TEMPLATE.read_text()

    assert "for event in initial_events[:6]" in html
    assert 'class="event-card' in html
    assert 'href="/?event={{ event.id }}"' in html
    assert "card_deposit = event.deposit" in html
    assert "card_total = event.full_price" in html
    assert "deposit today · ${{ '%.2f'|format(card_total) }} CAD total" in html
    assert '"price":"{{ e.full_price or e.price or 0 }}"' in html
    assert '<small>CAD total</small>' not in html


def test_live_landing_pages_use_compressed_images_and_real_review_attribution():
    templates = TEMPLATE.parent
    family = (templates / "landing_family_v2.html").read_text()
    maternity = (templates / "landing_maternity_v2.html").read_text()
    wedding = (templates / "landing_wedding_v5.html").read_text()

    assert "family-ss-9.webp" in family
    assert "wedding-ss-1.webp" in wedding
    assert "Sarah M." not in family + maternity + wedding
    assert "Michelle T." not in family + maternity + wedding
    assert "James &amp; Priya" not in wedding
    assert "Kateryna" in wedding
    assert "Nivi Varghese" in maternity
