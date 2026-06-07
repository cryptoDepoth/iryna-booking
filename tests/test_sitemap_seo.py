def test_public_sitemap_does_not_advertise_admin_login(client):
    resp = client.get('/sitemap.xml')
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert '<loc>https://book.pashynskaphoto.com/admin/login</loc>' not in body
    assert 'admin/login' not in body
    assert 'https://book.pashynskaphoto.com/' in body
    assert 'https://book.pashynskaphoto.com/privacy' in body
