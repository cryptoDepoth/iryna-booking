addEventListener('fetch', (event) => {
  event.respondWith(handleRequest(event.request));
});

async function handleRequest(request) {
  if (request.method !== 'POST') {
    return new Response('Method not allowed', { status: 405 });
  }

  const url = new URL(request.url);
  const pixelId = url.searchParams.get('pixelId');
  const eventName = url.searchParams.get('eventName');

  if (!pixelId || !eventName) {
    return new Response('Missing pixelId or eventName', { status: 400 });
  }

  try {
    const payload = await request.json();
    const metaEndpoint = `https://www.facebook.com/tr?id=${pixelId}&ev=${eventName}&dl=${encodeURIComponent(payload.dl || '')}&rl=${encodeURIComponent(payload.rl || '')}&if=${payload.if || false}&ts=${payload.ts || Date.now()}&cd[client_user_agent]=${encodeURIComponent(payload.userAgent || '')}`;

    // Forward to Meta
    await fetch(metaEndpoint, {
      method: 'GET',
      headers: { 'User-Agent': payload.userAgent || '' },
    });

    // Forward to Google Analytics (if needed)
    if (payload.googleAnalyticsId) {
      const gaEndpoint = `https://www.google-analytics.com/collect?v=1&tid=${payload.googleAnalyticsId}&cid=${payload.cid || '555'}&t=event&ec=${eventName}&ea=${payload.ea || 'booking_event'}&el=${payload.el || ''}&ev=${payload.ev || 1}`;
      await fetch(gaEndpoint, { method: 'POST' });
    }

    return new Response(JSON.stringify({ success: true }), {
      headers: { 'Content-Type': 'application/json' },
    });
  } catch (error) {
    return new Response(JSON.stringify({ success: false, error: error.message }), {
      status: 500,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}