from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_primary_admin_pages_share_one_navigation_shell():
    names = [
        "admin.html",
        "admin_event.html",
        "admin_clients.html",
        "admin_analytics.html",
        "admin_transfers.html",
        "admin_backup_center.html",
        "admin_link_generator.html",
        "booking_detail.html",
        "admin_health.html",
    ]
    for name in names:
        source = (ROOT / "templates" / name).read_text()
        assert "admin-shell.css" in source, name
        assert "{% include '_admin_shell.html' %}" in source, name


def test_admin_shell_links_every_operational_area():
    source = (ROOT / "templates" / "_admin_shell.html").read_text()
    for url in (
        "/admin",
        "/admin#events-section",
        "/admin/clients",
        "/admin/transfers",
        "/admin/analytics",
        "/admin/link-generator",
        "/admin/backup-center",
        "/admin/health-center",
    ):
        assert url in source


def test_health_center_is_authenticated_and_human_readable(client):
    anonymous = client.get("/admin/health-center")
    assert anonymous.status_code == 302
    assert "/admin/login" in anonymous.headers["Location"]

    with client.session_transaction() as session:
        session["admin_authenticated"] = True
    response = client.get("/admin/health-center")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "System health" in html
    assert "Gmail / Interac" in html
    assert "Notion sync" in html
    assert "Recovery copies" in html

