# Pinterest Developer App resubmit package — Pashynska Photography

App ID: `1578416`
Pinterest account: `@pashynskaphoto`

## Current API status

The provided 24h token is read-only and currently returns:

```text
401 {"code":3,"message":"Your application consumer type is not supported, please contact support."}
```

This matches Pinterest's review email: trial access is not approved yet, app secret is unavailable, and API access is blocked until the app review issue is fixed.

Available token scopes reported by Pinterest:

```text
pins:read
boards:read
user_accounts:read
ads:read
catalogs:read
```

Missing scopes for publishing:

```text
pins:write
boards:write
```

## Most likely rejection causes

Pinterest email listed three areas:

1. Website validation.
2. Privacy Policy validation.
3. App description completeness/accuracy.

Live audit findings:

- `https://book.pashynskaphoto.com/` is online and accessible.
- `https://book.pashynskaphoto.com/privacy` is online, but its visible title was more like "Privacy & Browser Storage" / "Simple privacy note", not clearly labeled "Privacy Policy".
- `https://book.pashynskaphoto.com/privacy-policy` returned 404 before the fix.
- `https://pashynskaphoto.com/privacy-policy` is online and clearly titled "Privacy Policy | Pashynska Photography".

## Fix made locally

To make the booking domain safer for Pinterest review, the Flask booking app was patched so:

```text
/privacy-policy
```

also serves the privacy page, and the English title/H1 now clearly say:

```text
Privacy Policy | Pashynska Photography
Privacy Policy
```

Tests run locally:

```text
15 passed
```

Deploy still needs to be run before resubmitting if using `book.pashynskaphoto.com/privacy-policy` in Pinterest app settings.

## Recommended app settings for resubmit

### Website URL

Preferred:

```text
https://pashynskaphoto.com
```

Reason: this is the main brand website, not just a booking tool, and Pinterest explicitly wants a website clearly associated with the company/application.

Acceptable fallback after deploy:

```text
https://book.pashynskaphoto.com
```

### Privacy Policy URL

Preferred:

```text
https://pashynskaphoto.com/privacy-policy
```

Fallback after booking-site deploy:

```text
https://book.pashynskaphoto.com/privacy-policy
```

### App name

```text
Pashynska Photography Pinterest Publisher
```

### App description

```text
Pashynska Photography is a Calgary-based photography business offering family, maternity, newborn, engagement, wedding and outdoor mini sessions in Calgary, Banff and Canmore.

This app is an internal publishing and content-management tool for the official Pinterest Business account @pashynskaphoto. It will be used by the business owner and authorized assistant only.

The app will use Pinterest API access to:
- read the business account profile to confirm it is connected to @pashynskaphoto;
- read existing boards and Pins to avoid duplicates;
- create and organize organic boards for photography topics such as Calgary Family Photography, Calgary Maternity Photography, Calgary Mini Sessions, Banff & Canmore Photo Sessions, and What to Wear for Family Photos;
- publish organic Pins using real Pashynska Photography portfolio images;
- add SEO-friendly Pin titles and descriptions;
- attach destination links to the official website and booking pages with UTM tracking.

The app will manage only Pashynska Photography's own Pinterest Business account. It will not manage third-party accounts, scrape users, send messages, post spam, or use Pinterest data for unrelated advertising.
```

### Data use / privacy answer

```text
The app uses Pinterest data only to manage Pashynska Photography's own organic Pinterest content. It reads account, board and Pin data to organize content, avoid duplicate publishing, and report on the business's own content. It creates organic Pins and boards only for the official @pashynskaphoto account.

Data is not sold, shared with third parties, or used for unrelated advertising. All destination links point to Pashynska Photography's official website or booking pages.
```

### Requested scopes

For read-only validation:

```text
user_accounts:read
boards:read
pins:read
```

For the actual automation we need:

```text
user_accounts:read
boards:read
boards:write
pins:read
pins:write
```

Do not request ads/catalogs yet unless Pinterest forces them from product-limited token defaults. Organic publishing is enough for now.

### Redirect URI

```text
http://localhost:8765/pinterest/oauth/callback
```

If Pinterest rejects localhost for review, use a production callback after adding it to the booking app:

```text
https://book.pashynskaphoto.com/admin/integrations/pinterest/callback
```

## Resubmit message

```text
Hello Pinterest Developer Support,

Thank you for reviewing our application. We updated the app submission to make the website, privacy policy, and app description clearer.

Website URL:
https://pashynskaphoto.com

Privacy Policy URL:
https://pashynskaphoto.com/privacy-policy

The website is the official Pashynska Photography business website, and the privacy policy is publicly accessible and clearly labeled as Privacy Policy on the same brand domain.

We also clarified the app description. The app is an internal tool used only by Pashynska Photography to manage the official Pinterest Business account @pashynskaphoto. It will read and organize our own boards and Pins, publish organic Pins using real portfolio images, and link those Pins to our official website and booking pages with UTM tracking.

The app will not manage third-party accounts, scrape users, send messages, post spam, or use Pinterest data for unrelated purposes.

Please review the updated application for Trial Access.
```
