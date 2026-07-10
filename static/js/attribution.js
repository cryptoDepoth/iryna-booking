(function () {
  'use strict';

  var STORAGE_KEY = 'pashynska_attribution_v1';
  var QUERY_KEYS = [
    'gclid', 'gbraid', 'wbraid', 'fbclid',
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term'
  ];

  function safeRead() {
    try {
      return JSON.parse(window.localStorage.getItem(STORAGE_KEY) || '{}') || {};
    } catch (_error) {
      return {};
    }
  }

  function safeWrite(value) {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
    } catch (_error) {
      // Attribution must never interrupt booking when storage is unavailable.
    }
  }

  function queryValue(name) {
    try {
      return (new URLSearchParams(window.location.search).get(name) || '').trim();
    } catch (_error) {
      return '';
    }
  }

  function cookieValue(name) {
    var prefix = name + '=';
    var cookies = document.cookie ? document.cookie.split(';') : [];
    for (var i = 0; i < cookies.length; i += 1) {
      var cookie = cookies[i].trim();
      if (cookie.indexOf(prefix) === 0) {
        var value = cookie.slice(prefix.length);
        try { return decodeURIComponent(value); } catch (_error) { return value; }
      }
    }
    return '';
  }

  function capture() {
    var stored = safeRead();
    var hasCampaignValue = false;
    QUERY_KEYS.forEach(function (key) {
      var value = queryValue(key);
      if (value) {
        stored[key] = value.slice(0, 200);
        hasCampaignValue = true;
      }
    });
    if (hasCampaignValue || !stored.landing_url) {
      stored.landing_url = window.location.href.slice(0, 500);
      stored.referrer = (document.referrer || '').slice(0, 500);
      stored.captured_at = Date.now();
    }
    safeWrite(stored);
    return stored;
  }

  function get() {
    var data = capture();
    data.fbp = cookieValue('_fbp') || data.fbp || '';
    data.fbc = cookieValue('_fbc') || data.fbc || '';
    if (!data.fbc && data.fbclid) {
      data.fbc = 'fb.1.' + String(data.captured_at || Date.now()) + '.' + data.fbclid;
    }
    return data;
  }

  function shouldDecorate(url) {
    return url.origin === window.location.origin && (
      url.pathname === '/' ||
      url.pathname === '/book' ||
      url.pathname.indexOf('/book/package/') === 0
    );
  }

  function decorateLinks(root) {
    var data = capture();
    (root || document).querySelectorAll('a[href]').forEach(function (link) {
      var url;
      try { url = new URL(link.href, window.location.href); } catch (_error) { return; }
      if (!shouldDecorate(url)) return;
      QUERY_KEYS.forEach(function (key) {
        if (data[key] && !url.searchParams.has(key)) url.searchParams.set(key, data[key]);
      });
      link.href = url.pathname + url.search + url.hash;
    });
  }

  window.PashynskaAttribution = {
    capture: capture,
    get: get,
    decorateLinks: decorateLinks
  };

  capture();
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { decorateLinks(document); });
  } else {
    decorateLinks(document);
  }
}());
