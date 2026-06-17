/* Pashynska Photography — Gift & Referral JS */
(function () {
  "use strict";

  // ---- Package selection ----
  function initPackageCards() {
    const cards = document.querySelectorAll(".gift-package-card");
    const customWrap = document.getElementById("gift-custom-amount-wrap");
    const priceValue = document.getElementById("gift-price-value");
    const priceSub   = document.getElementById("gift-price-sub");
    const sessionInput = document.getElementById("session_type");

    const PRICES = {
      mini:       { amount: 230.00, gst: 241.50 },
      family:     { amount: 290.00, gst: 304.50 },
      maternity:  { amount: 290.00, gst: 304.50 },
      individual: { amount: 290.00, gst: 304.50 },
      custom:     { amount: 0,      gst: 0 },
    };

    function selectCard(card) {
      cards.forEach(c => c.classList.remove("selected"));
      card.classList.add("selected");
      const type = card.dataset.type;
      if (sessionInput) sessionInput.value = type;

      if (customWrap) {
        customWrap.classList.toggle("visible", type === "custom");
      }

      if (type === "custom") {
        updatePriceFromCustom();
      } else {
        const p = PRICES[type] || { amount: 0, gst: 0 };
        if (priceValue) priceValue.textContent = `$${p.gst.toFixed(2)} CAD`;
        if (priceSub)   priceSub.textContent   = `($${p.amount.toFixed(2)} + 5% GST)`;
      }
    }

    function updatePriceFromCustom() {
      const customInput = document.getElementById("custom_amount");
      if (!customInput) return;
      const val = parseFloat(customInput.value) || 0;
      const gst = Math.round(val * 1.05 * 100) / 100;
      if (priceValue) priceValue.textContent = val >= 50 ? `$${gst.toFixed(2)} CAD` : "—";
      if (priceSub)   priceSub.textContent   = val >= 50 ? `($${val.toFixed(2)} + 5% GST)` : "";
    }

    cards.forEach(card => {
      card.addEventListener("click", () => selectCard(card));
      // keyboard
      card.setAttribute("tabindex", "0");
      card.setAttribute("role", "radio");
      card.addEventListener("keydown", e => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectCard(card); }
      });
    });

    const customInput = document.getElementById("custom_amount");
    if (customInput) {
      customInput.addEventListener("input", updatePriceFromCustom);
    }

    // Select first card by default
    if (cards.length > 0) selectCard(cards[0]);
  }

  // ---- Gift code validation (on booking form) ----
  function initGiftValidation() {
    const input   = document.getElementById("gift-code-input");
    const btn     = document.getElementById("gift-validate-btn");
    const status  = document.getElementById("gift-code-status");
    if (!input || !btn || !status) return;

    async function validate() {
      const code = input.value.trim().toUpperCase();
      if (!code) {
        status.className = "gift-code-status";
        status.textContent = "";
        return;
      }

      btn.disabled = true;
      btn.textContent = "Checking…";
      status.className = "gift-code-status";
      status.textContent = "";

      try {
        const res  = await fetch("/validate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ code }),
        });
        const data = await res.json();
        if (data.valid) {
          status.className = "gift-code-status valid";
          if (data.type === "gift") {
            const session = data.session_type ? ` · ${data.session_type} session` : "";
            status.textContent = `✓ Gift certificate valid${session} — $${(data.amount_with_gst || 0).toFixed(2)} applied`;
          } else if (data.type === "referral") {
            status.textContent = `✓ Referral code from ${data.owner_name} — $${(data.discount || 0).toFixed(0)} off applied`;
          } else {
            status.textContent = "✓ Code applied!";
          }
          // Store validated code in a hidden input if present
          const hidden = document.getElementById("promo_code_applied");
          if (hidden) hidden.value = code;
        } else {
          status.className = "gift-code-status invalid";
          status.textContent = `✗ ${data.error || "Invalid code"}`;
        }
      } catch {
        status.className = "gift-code-status invalid";
        status.textContent = "✗ Could not check code. Please try again.";
      } finally {
        btn.disabled = false;
        btn.textContent = "Apply";
      }
    }

    btn.addEventListener("click", validate);
    input.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); validate(); } });

    // Auto-upcase while typing
    input.addEventListener("input", () => {
      const pos = input.selectionStart;
      input.value = input.value.toUpperCase();
      input.setSelectionRange(pos, pos);
      // Clear status while user is typing
      status.className = "gift-code-status";
    });
  }

  // ---- Copy to clipboard ----
  function initCopyButtons() {
    document.querySelectorAll("[data-copy]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const text = btn.dataset.copy || btn.previousElementSibling?.textContent || "";
        try {
          await navigator.clipboard.writeText(text.trim());
          const original = btn.textContent;
          btn.textContent = "Copied!";
          btn.classList.add("copied");
          setTimeout(() => {
            btn.textContent = original;
            btn.classList.remove("copied");
          }, 2200);
        } catch {
          // Fallback
          const ta = document.createElement("textarea");
          ta.value = text.trim();
          document.body.appendChild(ta);
          ta.select();
          document.execCommand("copy");
          document.body.removeChild(ta);
          btn.textContent = "Copied!";
          setTimeout(() => { btn.textContent = "Copy"; }, 2000);
        }
      });
    });
  }

  // ---- WhatsApp / SMS share ----
  function initShareButtons() {
    const waBtn  = document.getElementById("share-whatsapp");
    const smsBtn = document.getElementById("share-sms");
    const msgEl  = document.getElementById("share-message");
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

  // ---- Form submit guard ----
  function initFormGuard() {
    const form = document.getElementById("gift-purchase-form");
    const btn  = document.getElementById("gift-submit-btn");
    if (!form || !btn) return;

    form.addEventListener("submit", (e) => {
      const sessionType = document.getElementById("session_type")?.value;
      if (sessionType === "custom") {
        const amt = parseFloat(document.getElementById("custom_amount")?.value || "0");
        if (amt < 50 || amt > 500) {
          e.preventDefault();
          alert("Custom amount must be between $50 and $500.");
          return;
        }
      }
      btn.disabled = true;
      btn.textContent = "Processing…";
    });
  }

  // ---- Init all ----
  document.addEventListener("DOMContentLoaded", () => {
    initPackageCards();
    initGiftValidation();
    initCopyButtons();
    initShareButtons();
    initFormGuard();
  });
})();
