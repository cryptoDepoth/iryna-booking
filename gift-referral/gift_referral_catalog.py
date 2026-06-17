"""Gift certificate package catalog shared by routes, PDFs, and emails."""

GST_RATE = 0.05

GIFT_PACKAGES = {
    "mini": {
        "label": "Mini Session",
        "short_label": "Mini Session",
        "amount": 210.00,
        "duration": "20 minutes",
        "photos": "15 edited photos",
        "details": "20 minutes · 15 edited photos · all original photos included",
        "icon": "M",
    },
    "family": {
        "label": "Individual / Family Session",
        "short_label": "Family Session",
        "amount": 320.00,
        "duration": "1 hour",
        "photos": "25 edited photos",
        "details": "1 hour · 25 edited photos · all original photos included",
        "icon": "F",
    },
    "maternity": {
        "label": "Maternity Session",
        "short_label": "Maternity Session",
        "amount": 320.00,
        "duration": "1 hour",
        "photos": "25 edited photos",
        "details": "1 hour · 25 edited photos · all original photos included",
        "icon": "MA",
    },
    "newborn": {
        "label": "Newborn Lifestyle Session",
        "short_label": "Newborn Session",
        "amount": 350.00,
        "duration": "1 hour",
        "photos": "30 edited photos",
        "details": "1 hour · 30 edited photos · all original photos included",
        "icon": "N",
    },
    "custom": {
        "label": "Custom Gift Certificate",
        "short_label": "Custom Package",
        "amount": 210.00,
        "duration": "Personalized",
        "photos": "Build your package",
        "details": "Choose a base experience and add personal upgrades",
        "icon": "C",
    },
}

CUSTOM_BASES = {
    "custom_30": {
        "label": "30-minute personalized session",
        "amount": 210.00,
        "details": "A flexible short session with all original photos included",
    },
    "custom_60": {
        "label": "1-hour personalized session",
        "amount": 320.00,
        "details": "A full individual, family, maternity, or creative session",
    },
    "custom_newborn": {
        "label": "Newborn lifestyle base",
        "amount": 350.00,
        "details": "A calm in-home newborn lifestyle experience",
    },
}

GIFT_ADD_ONS = {
    "extra_5_photos": {
        "label": "5 additional edited photos",
        "amount": 30.00,
    },
    "extra_10_photos": {
        "label": "10 additional edited photos",
        "amount": 50.00,
    },
    "highlight_video": {
        "label": "1-minute highlight video",
        "amount": 50.00,
    },
    "studio_session": {
        "label": "Studio session upgrade",
        "amount": 75.00,
    },
    "mountain_session": {
        "label": "Mountain session upgrade",
        "amount": 100.00,
    },
}

CERTIFICATE_STYLES = {
    "signature": {
        "label": "Signature Luxe",
        "description": "Dark charcoal, gold seal, evening luxury",
    },
    "ivory": {
        "label": "Ivory Editorial",
        "description": "Warm ivory, soft taupe, refined editorial",
    },
    "botanical": {
        "label": "Botanical Gold",
        "description": "Natural sage, ivory, soft gold accents",
    },
}

# Curated certificate photos per session type. Served from the same origin as /gift:
# /images is the booking event-photo store, /static/og-image.jpg is the brand image.
# A random photo from the matching list is shown on the certificate; if a specific
# image is ever removed, the front-end falls back to GIFT_PHOTO_FALLBACK automatically.
# Iryna can add/replace URLs here at any time — no other code changes needed.
GIFT_PHOTO_FALLBACK = "/static/og-image.jpg"

GIFT_PHOTOS = {
    "mini": [
        "/images/golden-boho-moments-2026-06-14_5367d2c1.jpeg",
        "/images/lilac-mini-session-2026-06-06_7f2e5bde.jpeg",
        "/images/swing-blossom-mini-session-2026-05-31_539e1b47.jpeg",
        "/images/mountains-mini-session-2026-06-20_44d5d944.png",
        "/images/canoe-mini-session-2026-07-04_5c96e407.png",
    ],
    "family": [
        "/images/individual-photoshoot-2026-06-04_ce3a657f.jpeg",
    ],
    "maternity": [GIFT_PHOTO_FALLBACK],
    "newborn": [GIFT_PHOTO_FALLBACK],
    "custom": [
        "/images/lilac-mini-session-2026-06-06_7f2e5bde.jpeg",
        "/images/golden-boho-moments-2026-06-14_5367d2c1.jpeg",
    ],
}


def calculate_with_gst(amount: float) -> float:
    return round(amount * (1 + GST_RATE), 2)


def public_catalog() -> dict:
    return {
        "packages": {
            key: {**value, "amount_with_gst": calculate_with_gst(value["amount"])}
            for key, value in GIFT_PACKAGES.items()
        },
        "custom_bases": {
            key: {**value, "amount_with_gst": calculate_with_gst(value["amount"])}
            for key, value in CUSTOM_BASES.items()
        },
        "add_ons": GIFT_ADD_ONS,
        "styles": CERTIFICATE_STYLES,
        "photos": GIFT_PHOTOS,
        "photo_fallback": GIFT_PHOTO_FALLBACK,
        "gst_rate": GST_RATE,
    }
