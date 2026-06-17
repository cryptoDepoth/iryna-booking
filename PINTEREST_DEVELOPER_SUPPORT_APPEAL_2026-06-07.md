# Pinterest Developer Support appeal — Pashynska Photography

App ID: `1578416`
Pinterest account: `@pashynskaphoto`
Website: `https://pashynskaphoto.com`
Privacy Policy: `https://pashynskaphoto.com/privacy-policy`
Booking site: `https://book.pashynskaphoto.com`

## Situation

Pinterest trial access was automatically denied shortly after app creation. App Secret is unavailable and write access is blocked. The app is intended for first-party/internal use only: managing Pashynska Photography's own Pinterest Business account and publishing organic Pins with real portfolio photos.

## Support form path

Open:

```text
https://help.pinterest.com/en/contact
```

Choose the closest category:

```text
Pinterest API / Developers / Developer Platform
```

If the form asks for a product, choose:

```text
Pinterest API
```

## Suggested subject

```text
Request review for denied Trial Access — App ID 1578416 — Pashynska Photography
```

## Suggested message

```text
Hello Pinterest Developer Support,

Our Pinterest Developer App trial access was automatically denied shortly after creation.

App ID: 1578416
Pinterest Business account: @pashynskaphoto
Business website: https://pashynskaphoto.com
Privacy Policy: https://pashynskaphoto.com/privacy-policy

Pashynska Photography is a Calgary-based photography business offering family, maternity, newborn, engagement, wedding and outdoor mini sessions in Calgary, Banff and Canmore.

This app is not for third-party account management, scraping, spam, messaging, or unrelated advertising. It is an internal first-party tool for our own official Pinterest Business account @pashynskaphoto.

The intended use is to:
- read our own Pinterest profile to confirm the connected account;
- read our own boards and Pins to avoid duplicates;
- create and organize organic boards for photography topics;
- publish organic Pins using real Pashynska Photography portfolio images;
- add SEO-friendly Pin titles and descriptions;
- attach destination links to our official website and booking pages with UTM tracking.

Requested scopes:
user_accounts:read
boards:read
boards:write
pins:read
pins:write

We have updated the website and privacy policy information to make the business connection clear. The privacy policy is publicly accessible and clearly labeled on the business domain:
https://pashynskaphoto.com/privacy-policy

Could you please review App ID 1578416 manually or advise what exact requirement is still missing for Trial Access approval?

Thank you,
Pashynska Photography
```

## Account activity warm-up before resubmission

Pinterest may auto-deny brand-new inactive accounts. Before resubmitting again, build real account history:

1. Add at least 30 real Pins.
2. Put at least 2-3 Pins on the most important boards:
   - Calgary Family Photography
   - Calgary Maternity Photography
   - Calgary Mini Sessions
   - Banff & Canmore Photo Sessions
   - Canoe & Lake Photo Sessions
   - Pashynska Photography Portfolio
3. Use only real Pashynska Photography images.
4. Use destination links to official domains:
   - `https://pashynskaphoto.com`
   - `https://book.pashynskaphoto.com/?utm_source=pinterest...`
5. Claim/verify the website in Pinterest Business settings if available.
6. Keep profile active for 2-4 weeks, then resubmit.

## API workaround while denied

Until Pinterest approves Trial Access:

- Do not spend time building API publisher scripts — tokens will fail with 401/consumer type errors.
- Use browser/manual publishing via Pinterest UI/Comet.
- Keep all Pin metadata in a local CSV/JSON queue so it can be reused by API later.

Recommended local queue columns:

```text
image_path, board_name, title, description, destination_url, status, pinterest_pin_url
```

When API gets approved, the same queue can be published programmatically.
