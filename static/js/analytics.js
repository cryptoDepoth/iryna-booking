// Custom events for booking funnel
// Pixel id comes from window.__META_PIXEL_ID (set by the server-rendered <head>)
// so it stays in lockstep with the rest of the funnel; literal is a safe fallback.
window.trackBookingEvent = function(eventName, params = {}) {
  var pixelId = window.__META_PIXEL_ID || '1335137335347797';
  var bookingSendTo = window.__GOOGLE_ADS_BOOKING_SEND_TO || '';
  var visitorId = '';
  try {
    visitorId = window.localStorage.getItem('pp_visitor_id') || '';
    if (!visitorId) {
      visitorId = 'v_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 10);
      window.localStorage.setItem('pp_visitor_id', visitorId);
    }
  } catch (_error) {
    visitorId = window.__ppVisitorId || ('v_' + Date.now().toString(36));
    window.__ppVisitorId = visitorId;
  }
  var attribution = window.PashynskaAttribution && window.PashynskaAttribution.get
    ? window.PashynskaAttribution.get() : {};
  var gtagMapping = {
    'payment_view':   { event: 'begin_checkout' },
    'booking_confirmed': { event: 'conversion', send_to: bookingSendTo, value: params.amount || params.value || 0, currency: 'CAD' },
    'purchase':       { event: 'conversion', send_to: bookingSendTo, value: params.amount || params.value || 0, currency: 'CAD' }
  };
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

  // First-party record keeps WhatsApp and funnel signals visible even when ad
  // platform scripts are blocked. It never delays the click/navigation.
  fetch('/track', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    keepalive: true,
    body: JSON.stringify(Object.assign({
      visitor_id: visitorId,
      event_name: eventName,
      event_id: params.event || '',
      page: window.location.pathname + window.location.search,
      metadata: params,
      referrer: document.referrer || '',
      landing_url: window.location.href
    }, attribution))
  }).catch(function () {});

  // Fallback: direct Meta Pixel (if Worker fails)
  if (window.fbq) {
    fbq('track', eventName, params);
  }
  if (window.gtag) {
    var mapping = gtagMapping[eventName];
    if (mapping) {
      var send = Object.assign({}, params);
      if (mapping.event === 'conversion' && !mapping.send_to) return;
      if (mapping.send_to) send.send_to = mapping.send_to;
      if (mapping.value !== undefined) send.value = mapping.value;
      if (mapping.currency) send.currency = mapping.currency;
      // transaction_id helps de-duplicate real purchases
      if (eventName === 'booking_confirmed' || eventName === 'purchase') {
        var transactionId = params.booking_id || params.transaction_id;
        if (transactionId) send.transaction_id = transactionId;
      }
      gtag('event', mapping.event, send);
      console.log('[Analytics] gtag conversion:', mapping.event, send);
    } else {
      gtag('event', eventName, params);
    }
  }
  console.log('[Analytics]', eventName, params);
};

// Example usage:
// trackBookingEvent('drawer_open', { event: 'mountain_mini' });
// trackBookingEvent('slot_selected', { event: 'mountain_mini', time: '10:00' });
// trackBookingEvent('reserve_attempt', { event: 'mountain_mini', email: 'client@example.com' });
// trackBookingEvent('confirmed_booking', { event: 'mountain_mini', amount: 241.5 });
