"""Public gift landing page pricing and search metadata regressions."""

import json
import re


def test_gift_landing_has_canonical_and_valid_product_schema(client):
    response = client.get("/gift")
    assert response.status_code == 200

    html = response.get_data(as_text=True)
    assert '<link rel="canonical" href="https://book.pashynskaphoto.com/gift">' in html

    blocks = re.findall(
        r'<script type="application/ld\+json">\s*(.*?)\s*</script>',
        html,
        flags=re.DOTALL,
    )
    schemas = [json.loads(block) for block in blocks]
    product = next(schema for schema in schemas if schema.get("@type") == "Product")
    assert product["offers"]["priceCurrency"] == "CAD"
    assert product["offers"]["lowPrice"] == "220.50"
    assert product["offers"]["highPrice"] == "367.50"


def test_gift_landing_shows_current_family_and_maternity_prices(client):
    html = client.get("/gift").get_data(as_text=True)
    assert "Family Session" in html
    assert "Maternity Session" in html
    assert html.count("$340.00") >= 2
