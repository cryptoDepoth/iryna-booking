// Custom events for booking funnel
// Pixel id comes from window.__META_PIXEL_ID (set by the server-rendered <head>)
// so it stays in lockstep with the rest of the funnel; literal is a safe fallback.
window.trackBookingEvent = function(eventName, params = {}) {
  var pixelId = window.__META_PIXEL_ID || '1335137335347797';
  // Send to Cloudflare Worker (bypass ad blockers)
  fetch(`https://meta-pixel-proxy.andreygongalo.workers.dev/?pixelId=${pixelId}&eventName=${eventName}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      dl: window.location.href,
      rl: document.referrer,
      userAgent: navigator.userAgent,
      ...params
    }),
  }).catch(err => console.error('[Analytics] Worker failed:', err));

  // Fallback: direct Meta Pixel (if Worker fails)
  if (window.fbq) {
    fbq('track', eventName, params);
  }
  if (window.gtag) {
    gtag('event', eventName, params);
  }
  console.log('[Analytics]', eventName, params);
};

// Example usage:
// trackBookingEvent('drawer_open', { event: 'mountain_mini' });
// trackBookingEvent('slot_selected', { event: 'mountain_mini', time: '10:00' });
// trackBookingEvent('reserve_attempt', { event: 'mountain_mini', email: 'client@example.com' });
// trackBookingEvent('confirmed_booking', { event: 'mountain_mini', amount: 241.5 });