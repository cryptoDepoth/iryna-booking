# Meta Pixel Proxy Worker

Proxy for sending Meta Pixel and Google Analytics events via Cloudflare Workers.

## Why?
- Bypass ad blockers (direct `connect.facebook.net` calls are often blocked).
- Centralize event tracking logic.
- Avoid exposing Meta Pixel ID in frontend code.

## Deployment
```bash
cd workers/meta-pixel-proxy
npm install
npm run deploy
```

## Usage
```javascript
// Frontend example
fetch(`https://meta-pixel-proxy.your-worker.workers.dev/?pixelId=YOUR_PIXEL_ID&eventName=PageView`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    dl: window.location.href,
    rl: document.referrer,
    userAgent: navigator.userAgent,
    googleAnalyticsId: 'UA-XXXXXX-Y', // optional
  }),
});
```

## Environment Variables
None required.

## Events
Supported events:
- `PageView`
- `ViewContent`
- `AddToCart`
- `InitiateCheckout`
- `Purchase`
- Custom: `drawer_open`, `slot_selected`, `reserve_attempt`, `confirmed_booking`

## Testing
1. Deploy the worker.
2. Send a test event:
```bash
curl -X POST "https://meta-pixel-proxy.your-worker.workers.dev/?pixelId=1234567890&eventName=PageView" \
  -H "Content-Type: application/json" \
  -d '{"dl": "https://book.pashynskaphoto.com", "userAgent": "test"}'
```
3. Check Meta Events Manager for the event.