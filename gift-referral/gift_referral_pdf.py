"""
Gift Certificate PDF generation using reportlab.
Square 148×148 mm, brand colors: beige #FAF7F2, dark #2C2C2C, gold #C4973A.
"""

import io
import json
import os
import textwrap
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

BEIGE      = colors.HexColor("#FAF7F2")
DARK       = colors.HexColor("#2C2C2C")
GOLD       = colors.HexColor("#C4973A")
GOLD_LIGHT = colors.HexColor("#E8D5A3")
LIGHT_BOX  = colors.HexColor("#F0EBE0")
GREY       = colors.HexColor("#888888")
GREY_DARK  = colors.HexColor("#666666")

SESSION_LABELS = {
    "mini":       "Mini Session (20 min · 15 edited photos)",
    "family":     "Individual / Family Session (1 hour · 25 edited photos)",
    "individual": "Individual / Family Session (1 hour · 25 edited photos)",
    "maternity":  "Maternity Session (1 hour · 25 edited photos)",
    "newborn":    "Newborn Lifestyle Session (1 hour · 30 edited photos)",
    "custom":     "Custom Gift Certificate",
}

PDF_DIR = os.environ.get("GIFT_PDF_DIR", os.path.join(os.path.dirname(__file__), "pdfs"))


def generate_gift_certificate_pdf(cert: dict) -> bytes:
    """Return raw PDF bytes for the given cert dict."""
    buf = io.BytesIO()
    size = 148 * mm  # square
    c = canvas.Canvas(buf, pagesize=(size, size))
    c.setPageCompression(0)  # keep streams uncompressed so code is searchable in bytes

    _draw_certificate(c, cert, size, size)

    c.save()
    return buf.getvalue()


DARK_BG    = colors.HexColor("#181410")  # near-black for dark luxury certificate
DARK_BG2   = colors.HexColor("#1e1a15")  # slightly lighter for code box

STYLE_PALETTES = {
    "signature": {
        "bg": DARK_BG,
        "box": DARK_BG2,
        "fg": BEIGE,
        "muted": GOLD_LIGHT,
        "soft": colors.HexColor("#8d7a56"),
        "accent": GOLD,
    },
    "ivory": {
        "bg": BEIGE,
        "box": colors.HexColor("#FFFFFF"),
        "fg": DARK,
        "muted": GREY_DARK,
        "soft": colors.HexColor("#b09a60"),
        "accent": GOLD,
    },
    "botanical": {
        "bg": colors.HexColor("#E9EDDF"),
        "box": colors.HexColor("#F7F4EA"),
        "fg": colors.HexColor("#26342D"),
        "muted": colors.HexColor("#5F6B5B"),
        "soft": colors.HexColor("#A99B63"),
        "accent": colors.HexColor("#8F7A3D"),
    },
}


def _palette(cert: dict) -> dict:
    return STYLE_PALETTES.get(cert.get("certificate_style") or "signature", STYLE_PALETTES["signature"])


def _addons_line(cert: dict) -> str:
    try:
        addons = json.loads(cert.get("addons_json") or "[]")
    except (TypeError, ValueError):
        addons = []
    labels = [str(addon.get("label", "")).strip() for addon in addons if addon.get("label")]
    return " + ".join(labels[:3])


def _draw_certificate(c: canvas.Canvas, cert: dict, w: float, h: float) -> None:
    palette = _palette(cert)
    bg = palette["bg"]
    box = palette["box"]
    fg = palette["fg"]
    muted = palette["muted"]
    soft = palette["soft"]
    accent = palette["accent"]

    # --- Dark luxury background ---
    c.setFillColor(bg)
    c.rect(0, 0, w, h, fill=1, stroke=0)

    # --- Outer gold border ---
    c.setStrokeColor(accent)
    c.setLineWidth(2)
    c.rect(7 * mm, 7 * mm, w - 14 * mm, h - 14 * mm, fill=0, stroke=1)

    # --- Inner hairline border ---
    c.setStrokeColor(soft)
    c.setLineWidth(0.4)
    c.rect(10 * mm, 10 * mm, w - 20 * mm, h - 20 * mm, fill=0, stroke=1)

    # --- Corner L-brackets ---
    for (cx, cy, sx, sy) in [
        (12*mm, h-12*mm, 1, -1), (w-12*mm, h-12*mm, -1, -1),
        (12*mm,   12*mm, 1,  1), (w-12*mm,   12*mm, -1,  1),
    ]:
        c.setStrokeColor(accent)
        c.setLineWidth(1.2)
        c.line(cx, cy, cx + sx * 5 * mm, cy)
        c.line(cx, cy, cx, cy + sy * 5 * mm)

    # --- Gold dashed seal ring ---
    seal_x, seal_y = w / 2, h - 37 * mm
    seal_r = 10 * mm
    c.setStrokeColor(accent)
    c.setLineWidth(0.7)
    c.setDash(2, 4)
    c.circle(seal_x, seal_y, seal_r, fill=0, stroke=1)
    c.setDash()  # reset
    # inner solid ring
    c.setStrokeColor(soft)
    c.setLineWidth(0.3)
    c.circle(seal_x, seal_y, seal_r - 2 * mm, fill=0, stroke=1)
    # tiny gold disk centre
    c.setFillColor(accent)
    c.circle(seal_x, seal_y, 1 * mm, fill=1, stroke=0)

    # --- Brand name ---
    c.setFillColor(accent)
    c.setFont("Helvetica-Bold", 9)
    c.drawCentredString(w / 2, h - 20 * mm, "PASHYNSKA PHOTOGRAPHY")

    # --- Thin gold rule ---
    c.setStrokeColor(accent)
    c.setLineWidth(0.6)
    c.line(30 * mm, h - 23 * mm, w - 30 * mm, h - 23 * mm)

    # --- "GIFT CERTIFICATE" title (below seal) ---
    c.setFillColor(fg)
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(w / 2, h - 53 * mm, "GIFT CERTIFICATE")

    # --- Diamond row ---
    c.setFillColor(accent)
    for dx in (-10 * mm, 0, 10 * mm):
        cx2, cy2 = w / 2 + dx, h - 58 * mm
        p = c.beginPath()
        p.moveTo(cx2, cy2 + 1.4 * mm)
        p.lineTo(cx2 + 1.4 * mm, cy2)
        p.lineTo(cx2, cy2 - 1.4 * mm)
        p.lineTo(cx2 - 1.4 * mm, cy2)
        p.close()
        c.drawPath(p, fill=1, stroke=0)

    # --- "This certificate is lovingly gifted to" ---
    c.setFillColor(muted)
    c.setFont("Helvetica-Oblique", 8)
    c.drawCentredString(w / 2, h - 66 * mm, "This certificate is lovingly gifted to")

    # --- Recipient name ---
    recipient = (cert.get("recipient_name") or "").strip() or "the bearer"
    c.setFillColor(fg)
    c.setFont("Times-Italic", 17)
    c.drawCentredString(w / 2, h - 75 * mm, recipient)

    # --- Thin rule under recipient ---
    c.setStrokeColor(soft)
    c.setLineWidth(0.4)
    c.line(32 * mm, h - 78 * mm, w - 32 * mm, h - 78 * mm)

    # --- "Entitles the holder to a" ---
    c.setFillColor(muted)
    c.setFont("Helvetica", 8)
    c.drawCentredString(w / 2, h - 85 * mm, "Entitles the holder to a")

    # --- Session type ---
    session_label = cert.get("package_label") or SESSION_LABELS.get(cert.get("session_type") or "custom", "Photography Session")
    c.setFillColor(accent)
    c.setFont("Helvetica-Bold", 10.5 if len(session_label) > 46 else 12)
    c.drawCentredString(w / 2, h - 93 * mm, session_label[:72])

    c.setFillColor(muted)
    c.setFont("Helvetica", 8)
    c.drawCentredString(w / 2, h - 98 * mm, "with Pashynska Photography · Calgary, AB")

    addons_line = _addons_line(cert)
    if addons_line:
        c.setFillColor(soft)
        c.setFont("Helvetica", 6.5)
        c.drawCentredString(w / 2, h - 102 * mm, addons_line[:82])

    # --- Value ---
    c.setFillColor(accent)
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(
        w / 2, h - 108 * mm,
        f"Value: ${cert['amount_with_gst']:.2f} CAD (incl. GST)"
    )

    # --- Code box (dark surface with gold border) ---
    box_y = h - 121 * mm
    c.setFillColor(box)
    c.roundRect(22 * mm, box_y, w - 44 * mm, 12 * mm, 2 * mm, fill=1, stroke=0)
    c.setStrokeColor(accent)
    c.setLineWidth(0.8)
    c.roundRect(22 * mm, box_y, w - 44 * mm, 12 * mm, 2 * mm, fill=0, stroke=1)

    c.setFillColor(accent)
    c.setFont("Courier-Bold", 14)
    c.drawCentredString(w / 2, box_y + 3.8 * mm, cert["code"])

    c.setFillColor(muted)
    c.setFont("Helvetica", 7)
    c.drawCentredString(w / 2, h - 124.5 * mm, "Enter this code when booking your session")

    # --- Valid until ---
    expires = cert.get("expires_at", "")
    c.setFillColor(fg)
    c.setFont("Helvetica", 8)
    c.drawCentredString(w / 2, h - 131 * mm, f"Valid until: {expires}")

    # --- Personal message ---
    msg = (cert.get("personal_message") or "").strip()
    if msg:
        lines = textwrap.wrap(msg, 50)[:2]
        c.setFillColor(muted)
        c.setFont("Helvetica-Oblique", 7.5)
        for i, line in enumerate(lines):
            if i == 0 and len(lines) == 1:
                line = f'"{line}"'
            elif i == 0:
                line = f'"{line}'
            elif i == len(lines) - 1:
                line = f'{line}"'
            c.drawCentredString(w / 2, h - (136 + i * 8) * mm, line)

    # --- Bottom rule ---
    c.setStrokeColor(accent)
    c.setLineWidth(0.6)
    c.line(28 * mm, h - 141 * mm, w - 28 * mm, h - 141 * mm)

    # --- Website ---
    c.setFillColor(accent)
    c.setFont("Helvetica-Bold", 8)
    c.drawCentredString(w / 2, h - 144 * mm, "book.pashynskaphoto.com")

    c.setFillColor(muted)
    c.setFont("Helvetica", 6.5)
    c.drawCentredString(w / 2, h - 147.5 * mm, "irynapashynska@gmail.com  ·  Calgary, AB")


def save_gift_pdf(cert: dict) -> str:
    """Generate PDF, save to pdfs/ dir, return file path."""
    os.makedirs(PDF_DIR, exist_ok=True)
    filename = f"{cert['code']}.pdf"
    path = os.path.join(PDF_DIR, filename)
    pdf_bytes = generate_gift_certificate_pdf(cert)
    with open(path, "wb") as f:
        f.write(pdf_bytes)
    return path
