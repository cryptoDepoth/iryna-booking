/* Pashynska Photography - Gift & Referral JS */
(function () {
  "use strict";

  function readGiftConfig() {
    const node = document.getElementById("gift-config");
    if (!node) return null;
    try {
      return JSON.parse(node.textContent || "{}");
    } catch {
      return null;
    }
  }

  const CONFIG = readGiftConfig();
  const GST_RATE = CONFIG?.gst_rate ?? 0.05;
  const PHOTOS = (CONFIG && CONFIG.photos) || {};
  const PHOTO_FALLBACK = (CONFIG && CONFIG.photo_fallback) || "/static/og-image.jpg";
  let currentPhoto = "";

  function money(value) {
    return (Math.round((Number(value) || 0) * 100) / 100).toFixed(2);
  }

  function clean(value, fallback) {
    const text = (value || "").trim();
    return text || fallback;
  }

  function photoPool(type) {
    const pool = PHOTOS[type];
    return (Array.isArray(pool) && pool.length) ? pool : [PHOTO_FALLBACK];
  }

  function randomPhoto(type) {
    const pool = photoPool(type);
    return pool[Math.floor(Math.random() * pool.length)];
  }

  function applyPhoto(url) {
    currentPhoto = url || PHOTO_FALLBACK;
    const img = document.getElementById("gift-preview-photo");
    const hidden = document.getElementById("gift_photo");
    if (img) img.src = currentPhoto;
    if (hidden) hidden.value = currentPhoto;
  }

  function selectedAddonIds() {
    return Array.from(document.querySelectorAll('input[name="gift_addons"]:checked'))
      .map(input => input.value);
  }

  function selectedAddonData() {
    return selectedAddonIds()
      .map(id => ({ id, ...(CONFIG?.add_ons?.[id] || {}) }))
      .filter(addon => addon.label);
  }

  function currentState() {
    const sessionType = document.getElementById("session_type")?.value || "mini";
    const customBase = document.querySelector('input[name="custom_base"]:checked')?.value || "custom_30";
    const paymentMethod = document.querySelector('input[name="payment_method"]:checked')?.value || "card";
    const packageData = CONFIG?.packages?.[sessionType] || {};
    const baseData = sessionType === "custom"
      ? (CONFIG?.custom_bases?.[customBase] || CONFIG?.custom_bases?.custom_30 || packageData)
      : packageData;
    const addons = selectedAddonData();
    const subtotal = Number(baseData.amount || 0) + addons.reduce((sum, addon) => sum + Number(addon.amount || 0), 0);
    const total = Math.round(subtotal * (1 + GST_RATE) * 100) / 100;
    return { sessionType, customBase, paymentMethod, packageData, baseData, addons, subtotal, total };
  }

  function updatePriceAndPreview() {
    const state = currentState();
    const priceValue = document.getElementById("gift-price-value");
    const priceSub = document.getElementById("gift-price-sub");
    const submitBtn = document.getElementById("gift-submit-btn");
    const paymentNote = document.getElementById("gift-payment-note");

    if (priceValue) priceValue.textContent = `$${money(state.total)} CAD`;
    if (priceSub) priceSub.textContent = `($${money(state.subtotal)} + 5% GST)`;
    if (submitBtn) {
      const label = state.paymentMethod === "interac"
        ? "Continue to e-Transfer"
        : "Purchase Gift Certificate";
      submitBtn.textContent = `${label} - $${money(state.total)} CAD`;
    }
    if (paymentNote) {
      paymentNote.textContent = state.paymentMethod === "interac"
        ? "Interac auto-deposit instructions are shown after checkout. PDF is emailed after payment confirmation."
        : "Secure checkout via Stripe. Receipt and PDF are emailed instantly after payment.";
    }

    const purchaserName = document.getElementById("purchaser_name");
    const recipientName = document.getElementById("recipient_name");
    const personalMessage = document.getElementById("personal_message");

    const previewTo = document.getElementById("gift-preview-to");
    const previewFrom = document.getElementById("gift-preview-from");
    const previewSession = document.getElementById("gift-preview-session");
    const previewMeta = document.getElementById("gift-preview-package-meta");
    const previewAddons = document.getElementById("gift-preview-addons");
    const previewMessage = document.getElementById("gift-preview-message");
    const previewPrice = document.getElementById("gift-preview-price");

    if (previewTo) previewTo.textContent = clean(recipientName?.value, "Sarah");
    if (previewFrom) previewFrom.textContent = `With love, ${clean(purchaserName?.value, "Jane")}`;
    if (previewSession) {
      previewSession.textContent = state.sessionType === "custom"
        ? "Custom Gift Certificate"
        : clean(state.packageData.label, "Photography Session");
    }
    if (previewMeta) {
      const meta = state.sessionType === "custom"
        ? clean(state.baseData.details || state.baseData.label, "Personalized photography package")
        : clean(
            state.packageData.photos
              ? state.packageData.photos + " · all originals included"
              : state.packageData.details,
            "All original photos included"
          );
      previewMeta.textContent = meta;
    }

    const previewDuration = document.getElementById("gift-preview-duration");
    if (previewDuration) {
      const dur = clean(state.packageData.duration, "");
      previewDuration.innerHTML = dur ? ('<span aria-hidden="true">⏱</span> ' + dur) : "";
      previewDuration.style.display = dur ? "" : "none";
    }
    if (previewAddons) {
      previewAddons.textContent = state.addons.length
        ? state.addons.map(addon => addon.label).join(" + ")
        : "No upgrades selected";
    }
    if (previewMessage) {
      previewMessage.textContent = clean(
        personalMessage?.value,
        "You deserve to be seen in your most beautiful light."
      );
    }
    if (previewPrice) previewPrice.textContent = `$${money(state.total)} CAD`;
  }

  function initPackageCards() {
    const cards = Array.from(document.querySelectorAll(".gift-package-card"));
    const sessionInput = document.getElementById("session_type");
    const customWrap = document.getElementById("gift-custom-amount-wrap");
    if (!cards.length || !sessionInput) return;

    function selectCard(card) {
      const type = card.dataset.type || "mini";
      cards.forEach(item => {
        item.classList.toggle("selected", item === card);
        item.setAttribute("aria-checked", item === card ? "true" : "false");
      });
      const radio = card.querySelector('input[type="radio"]');
      if (radio) radio.checked = true;
      sessionInput.value = type;
      if (customWrap) customWrap.classList.toggle("visible", type === "custom");
      updatePriceAndPreview();
      document.dispatchEvent(new CustomEvent("gift:session-changed", { detail: { type } }));
    }

    cards.forEach(card => {
      card.setAttribute("tabindex", "0");
      card.setAttribute("role", "radio");
      card.addEventListener("click", () => selectCard(card));
      card.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectCard(card);
        }
      });
    });

    selectCard(cards.find(card => card.querySelector("input:checked")) || cards[0]);
  }

  function initOptionInputs() {
    document.querySelectorAll('input[name="custom_base"], input[name="gift_addons"]').forEach(input => {
      input.addEventListener("change", updatePriceAndPreview);
    });
  }

  function initPaymentMethods() {
    const cards = Array.from(document.querySelectorAll(".gift-payment-card"));
    if (!cards.length) return;

    function refresh() {
      cards.forEach(card => {
        const input = card.querySelector("input");
        card.classList.toggle("selected", Boolean(input?.checked));
      });
      updatePriceAndPreview();
    }

    cards.forEach(card => {
      card.addEventListener("click", () => {
        const input = card.querySelector("input");
        if (input) input.checked = true;
        refresh();
      });
    });
    document.querySelectorAll('input[name="payment_method"]').forEach(input => {
      input.addEventListener("change", refresh);
    });
    refresh();
  }

  function initStylePicker() {
    const wrapper = document.getElementById("gift-cert-preview-wrap");
    const input = document.getElementById("certificate_style");
    const name = document.getElementById("gift-style-name");
    const dots = document.getElementById("gift-style-dots");
    if (!wrapper || !input || !CONFIG?.styles) return;

    const preferredOrder = ["signature", "ivory", "botanical"];
    const styles = preferredOrder
      .filter(id => CONFIG.styles[id])
      .map(id => ({ id, ...CONFIG.styles[id] }));
    Object.entries(CONFIG.styles).forEach(([id, value]) => {
      if (!preferredOrder.includes(id)) styles.push({ id, ...value });
    });
    if (!styles.length) return;
    let index = Math.max(0, styles.findIndex(style => style.id === input.value));

    function renderDots() {
      if (!dots) return;
      dots.innerHTML = "";
      styles.forEach((style, dotIndex) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = dotIndex === index ? "active" : "";
        button.setAttribute("aria-label", style.label);
        button.addEventListener("click", () => setStyle(dotIndex));
        dots.appendChild(button);
      });
    }

    function setStyle(nextIndex) {
      index = (nextIndex + styles.length) % styles.length;
      const style = styles[index];
      input.value = style.id;
      wrapper.classList.remove(...styles.map(item => `style-${item.id}`));
      wrapper.classList.add(`style-${style.id}`);
      if (name) name.textContent = style.label;
      renderDots();
    }

    document.getElementById("gift-style-prev")?.addEventListener("click", () => setStyle(index - 1));
    document.getElementById("gift-style-next")?.addEventListener("click", () => setStyle(index + 1));

    let startX = null;
    wrapper.addEventListener("pointerdown", event => {
      startX = event.clientX;
    });
    wrapper.addEventListener("pointerup", event => {
      if (startX === null) return;
      const dx = event.clientX - startX;
      startX = null;
      if (Math.abs(dx) < 40) return;
      setStyle(index + (dx < 0 ? 1 : -1));
    });

    setStyle(index);
  }

  function initGiftPreview() {
    const fields = [
      "purchaser_name",
      "recipient_name",
      "personal_message",
    ];
    fields.forEach(id => {
      document.getElementById(id)?.addEventListener("input", updatePriceAndPreview);
    });

    const sparks = document.getElementById("gift-preview-sparks");
    if (
      sparks &&
      !(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches)
    ) {
      setInterval(() => {
        const spark = document.createElement("div");
        const x = Math.random() * 92 + 2;
        const size = Math.random() * 4 + 2;
        const duration = (Math.random() * 1.2 + 0.9).toFixed(2);
        spark.style.cssText = [
          "position:absolute",
          "bottom:0",
          `left:${x}%`,
          `width:${size}px`,
          `height:${size}px`,
          "background:#C4973A",
          "border-radius:50%",
          `animation:giftCertSparkPop ${duration}s ease-out forwards`,
        ].join(";");
        sparks.appendChild(spark);
        setTimeout(() => spark.remove(), (parseFloat(duration) + 0.2) * 1000);
      }, 380);
    }

    updatePriceAndPreview();
  }

  function initGiftPhotos() {
    const img = document.getElementById("gift-preview-photo");
    if (img) {
      img.addEventListener("error", function () {
        if (img.src.indexOf(PHOTO_FALLBACK) === -1) img.src = PHOTO_FALLBACK;
      });
    }
    document.addEventListener("gift:session-changed", function (event) {
      const type = (event.detail && event.detail.type) || "mini";
      applyPhoto(randomPhoto(type));
    });
    const shuffle = document.getElementById("gift-photo-shuffle");
    if (shuffle) {
      shuffle.addEventListener("click", function () {
        const type = document.getElementById("session_type")?.value || "mini";
        applyPhoto(randomPhoto(type));
      });
    }
    const initialType = document.getElementById("session_type")?.value || "mini";
    applyPhoto(randomPhoto(initialType));
  }

  function initGiftValidation() {
    const input = document.getElementById("gift-code-input");
    const btn = document.getElementById("gift-validate-btn");
    const status = document.getElementById("gift-code-status");
    if (!input || !btn || !status) return;

    async function validate() {
      const code = input.value.trim().toUpperCase();
      if (!code) {
        status.className = "gift-code-status";
        status.textContent = "";
        return;
      }

      btn.disabled = true;
      btn.textContent = "Checking...";
      status.className = "gift-code-status";
      status.textContent = "";

      try {
        const res = await fetch("/validate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ code }),
        });
        const data = await res.json();
        if (data.valid) {
          status.className = "gift-code-status valid";
          if (data.type === "gift") {
            const session = data.session_type ? ` · ${data.session_type} session` : "";
            status.textContent = `Gift certificate valid${session} - $${money(data.amount_with_gst || 0)} applied`;
          } else if (data.type === "referral") {
            status.textContent = `Referral code from ${data.owner_name} - $${(data.discount || 0).toFixed(0)} off applied`;
          } else {
            status.textContent = "Code applied.";
          }
          const hidden = document.getElementById("promo_code_applied");
          if (hidden) hidden.value = code;
        } else {
          status.className = "gift-code-status invalid";
          status.textContent = data.error || "Invalid code";
        }
      } catch {
        status.className = "gift-code-status invalid";
        status.textContent = "Could not check code. Please try again.";
      } finally {
        btn.disabled = false;
        btn.textContent = "Apply";
      }
    }

    btn.addEventListener("click", validate);
    input.addEventListener("keydown", event => {
      if (event.key === "Enter") {
        event.preventDefault();
        validate();
      }
    });
    input.addEventListener("input", () => {
      const pos = input.selectionStart;
      input.value = input.value.toUpperCase();
      input.setSelectionRange(pos, pos);
      status.className = "gift-code-status";
    });
  }

  function initCopyButtons() {
    document.querySelectorAll("[data-copy]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const target = btn.dataset.copy || "";
        const selected = target.startsWith("#") ? document.querySelector(target) : null;
        const text = selected?.textContent || target || btn.previousElementSibling?.textContent || "";
        try {
          await navigator.clipboard.writeText(text.trim());
          const original = btn.textContent;
          btn.textContent = "Copied";
          btn.classList.add("copied");
          setTimeout(() => {
            btn.textContent = original;
            btn.classList.remove("copied");
          }, 2200);
        } catch {
          const ta = document.createElement("textarea");
          ta.value = text.trim();
          document.body.appendChild(ta);
          ta.select();
          document.execCommand("copy");
          document.body.removeChild(ta);
        }
      });
    });
  }

  function initShareButtons() {
    const waBtn = document.getElementById("share-whatsapp");
    const smsBtn = document.getElementById("share-sms");
    const msgEl = document.getElementById("share-message");
    if (!msgEl) return;

    const msg = msgEl.dataset.message || msgEl.textContent.trim();
    const enc = encodeURIComponent(msg);

    if (waBtn) {
      waBtn.addEventListener("click", () => {
        window.open(`https://wa.me/?text=${enc}`, "_blank", "noopener");
      });
    }
    if (smsBtn) {
      smsBtn.addEventListener("click", () => {
        window.location.href = `sms:?body=${enc}`;
      });
    }
  }

  function initFormGuard() {
    const form = document.getElementById("gift-purchase-form");
    const btn = document.getElementById("gift-submit-btn");
    if (!form || !btn) return;

    form.addEventListener("submit", () => {
      btn.disabled = true;
      btn.textContent = "Processing...";
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    initPackageCards();
    initOptionInputs();
    initPaymentMethods();
    initStylePicker();
    initGiftPreview();
    initGiftPhotos();
    initGiftValidation();
    initCopyButtons();
    initShareButtons();
    initFormGuard();
  });
})();
