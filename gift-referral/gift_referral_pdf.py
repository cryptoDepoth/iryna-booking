"""
Gift Certificate PDF generation using reportlab.
Square 148×148 mm, brand colors: beige #FAF7F2, dark #2C2C2C, gold #C4973A.
"""

import io
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
    "mini":       "Mini Session (30 min · 20 photos)",
    "family":     "Family Session (1 hour · 30 photos)",
    "maternity":  "Maternity Session (1 hour · 30 photos)",
    "individual": "Individual Session (1 hour · 30 photos)",
    "custom":     "Photography Session",
}

PDF_DIR = os.path.join(os.path.dirname(__file__), "pdfs")


def generate_gift_certificate_pdf(cert: dict) -> bytes:
    """Return raw PDF bytes for the given cert dict."""
    buf = io.BytesIO()
    size = 148 * mm  # square
    c = canvas.Canvas(buf, pagesize=(size, size))
    c.setPageCompression(0)  # keep streams uncompressed so code is searchable in bytes

    _draw_certificate(c, cert, size, size)

    c.save()
    return buf.getvalue()


def _draw_certificate(c: canvas.Canvas, cert: dict, w: float, h: float) -> None:
    # --- Background ---
    c.setFillColor(BEIGE)
    c.rect(0, 0, w, h, fill=1, stroke=0)

    # --- Outer gold border ---
    c.setStrokeColor(GOLD)
    c.setLineWidth(2.5)
    c.rect(8 * mm, 8 * mm, w - 16 * mm, h - 16 * mm, fill=0, stroke=1)

    # --- Inner hairline border ---
    c.setStrokeColor(GOLD_LIGHT)
    c.setLineWidth(0.5)
    c.rect(10.5 * mm, 10.5 * mm, w - 21 * mm, h - 21 * mm, fill=0, stroke=1)

    # --- Corner flourishes (simple cross marks) ---
    for (cx, cy) in [(13*mm, 13*mm), (w-13*mm, 13*mm), (13*mm, h-13*mm), (w-13*mm, h-13*mm)]:
        c.setStrokeColor(GOLD)
        c.setLineWidth(0.8)
        c.line(cx - 2*mm, cy, cx + 2*mm, cy)
        c.line(cx, cy - 2*mm, cx, cy + 2*mm)

    # --- Brand name ---
    c.setFillColor(GOLD)
    c.setFont("Helvetica-Bold", 17)
    c.drawCentredString(w / 2, h - 24 * mm, "Pashynska Photography")

    # --- Gold rule under brand ---
    c.setStrokeColor(GOLD)
    c.setLineWidth(1)
    c.line(28 * mm, h - 27 * mm, w - 28 * mm, h - 27 * mm)

    # --- "GIFT CERTIFICATE" ---
    c.setFillColor(DARK)
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(w / 2, h - 39 * mm, "GIFT CERTIFICATE")

    # --- Decorative dots ---
    c.setFillColor(GOLD)
    for dx in (-14 * mm, 0, 14 * mm):
        c.circle(w / 2 + dx, h - 44.5 * mm, 0.9 * mm, fill=1, stroke=0)

    # --- "This certificate entitles" ---
    c.setFillColor(DARK)
    c.setFont("Helvetica", 9.5)
    c.drawCentredString(w / 2, h - 53 * mm, "This certificate entitles")

    # --- Recipient name ---
    recipient = (cert.get("recipient_name") or "").strip() or "the bearer"
    c.setFillColor(GOLD)
    c.setFont("Helvetica-Bold", 13)
    c.drawCentredString(w / 2, h - 61 * mm, recipient)

    # --- "to a" ---
    c.setFillColor(DARK)
    c.setFont("Helvetica", 9.5)
    c.drawCentredString(w / 2, h - 67 * mm, "to a")

    # --- Session type ---
    session_label = SESSION_LABELS.get(cert.get("session_type") or "custom", "Photography Session")
    c.setFillColor(DARK)
    c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(w / 2, h - 74 * mm, session_label)

    c.setFont("Helvetica", 9)
    c.drawCentredString(w / 2, h - 79 * mm, "with Pashynska Photography · Calgary, AB")

    # --- Value ---
    c.setFillColor(GOLD)
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(
        w / 2, h - 86 * mm,
        f"Value: ${cert['amount_with_gst']:.2f} CAD (incl. GST)"
    )

    # --- Code box ---
    box_y = h - 103 * mm
    c.setFillColor(LIGHT_BOX)
    c.roundRect(24 * mm, box_y, w - 48 * mm, 13 * mm, 2 * mm, fill=1, stroke=0)
    c.setStrokeColor(GOLD)
    c.setLineWidth(1)
    c.roundRect(24 * mm, box_y, w - 48 * mm, 13 * mm, 2 * mm, fill=0, stroke=1)

    c.setFillColor(DARK)
    c.setFont("Courier-Bold", 15)
    c.drawCentredString(w / 2, box_y + 4 * mm, cert["code"])

    c.setFillColor(GREY)
    c.setFont("Helvetica", 7.5)
    c.drawCentredString(w / 2, h - 106.5 * mm, "Enter this code when booking your session")

    # --- Valid until ---
    expires = cert.get("expires_at", "")
    c.setFillColor(DARK)
    c.setFont("Helvetica", 8.5)
    c.drawCentredString(w / 2, h - 114 * mm, f"Valid until: {expires}")

    # --- Personal message (if short enough) ---
    msg = (cert.get("personal_message") or "").strip()
    if msg:
        # Wrap to ~50 chars per line, max 2 lines
        lines = textwrap.wrap(msg, 50)[:2]
        c.setFillColor(GREY_DARK)
        c.setFont("Helvetica-Oblique", 7.5)
        for i, line in enumerate(lines):
            if i == 0 and len(lines) == 1:
                line = f'"{line}"'
            elif i == 0:
                line = f'"{line}'
            elif i == len(lines) - 1:
                line = f'{line}"'
            c.drawCentredString(w / 2, h - (120 + i * 8) * mm, line)

    # --- Bottom rule ---
    c.setStrokeColor(GOLD)
    c.setLineWidth(0.8)
    c.line(28 * mm, h - 127 * mm, w - 28 * mm, h - 127 * mm)

    # --- Website ---
    c.setFillColor(DARK)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawCentredString(w / 2, h - 133 * mm, "book.pashynskaphoto.com")

    c.setFillColor(GREY)
    c.setFont("Helvetica", 7)
    c.drawCentredString(w / 2, h - 137 * mm, "irynapashynska@gmail.com  ·  Calgary, AB")


def save_gift_pdf(cert: dict) -> str:
    """Generate PDF, save to pdfs/ dir, return file path."""
    os.makedirs(PDF_DIR, exist_ok=True)
    filename = f"{cert['code']}.pdf"
    path = os.path.join(PDF_DIR, filename)
    pdf_bytes = generate_gift_certificate_pdf(cert)
    with open(path, "wb") as f:
        f.write(pdf_bytes)
    return path
