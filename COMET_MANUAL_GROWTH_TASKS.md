# Comet manual growth tasks — Pashynska Photography

Goal: do only browser-only changes that Hermes should not force through APIs without a logged-in human session. Keep everything reversible. Do not change visual design/CSS unless explicitly requested.

## 0) Golden rules

- Canonical portfolio site: `https://pashynskaphoto.com/`
- Booking/cash-register site: `https://book.pashynskaphoto.com/`
- For bookable mini/family/maternity sessions, CTAs should go to the booking app, not Instagram DM.
- For weddings/custom inquiries, use inquiry/contact flow.
- Do not delete ads/pages/posts. Pause or draft only.
- Before changing public profile fields, note the current value in this file or a screenshot.

---

## 1) Google Business Profile — highest ROI

Open: https://business.google.com/

Find the Pashynska Photography profile.

### Update links

Set:

- Website: `https://pashynskaphoto.com/`
- Appointment / booking link, if available:
  `https://book.pashynskaphoto.com/?utm_source=google&utm_medium=organic&utm_campaign=gbp_booking&utm_content=appointment_link`

If Google has separate fields for social links, keep Instagram/Facebook as-is unless outdated.

### Services to add/check

Add or verify these services/categories under the Photographer profile:

- Family Photography — Calgary family photo sessions, outdoor or studio.
- Maternity Photography — maternity sessions in Calgary, Banff, Canmore, studio/outdoor.
- Newborn Photography — calm newborn and baby sessions.
- Engagement Photography — engagement/couples sessions in Calgary and mountains.
- Wedding Photography — small weddings and elopements in Calgary, Banff, Canmore.
- Mini Sessions — seasonal limited-time photo sessions.

### Products/packages to add/check

Add products/services with price labels if Google allows:

- Mini Sessions — from `$180–$230 + GST`
- Regular Session — `$290 + GST`
- Engagement Session — `$290 + GST`
- Small Wedding — `$580 + GST`
- Full Wedding — `$1160 + GST`

Button/CTA URL for bookable sessions:
`https://book.pashynskaphoto.com/?utm_source=google&utm_medium=organic&utm_campaign=gbp_product&utm_content=<package_slug>`

### Q&A seeds

Add these Q&A answers manually if owner Q&A is available:

1. Q: How much is a family photo session in Calgary?  
   A: Regular family sessions start at $290 + GST. Seasonal mini sessions are usually $180–$230 + GST depending on the setup and location.

2. Q: Can I book online?  
   A: Yes, available dates and session spots can be booked online at book.pashynskaphoto.com.

3. Q: Do you photograph maternity sessions?  
   A: Yes, maternity sessions are available in Calgary, studio, outdoor, Banff and Canmore-style locations depending on season and availability.

4. Q: Do you include original photos?  
   A: Yes, Pashynska Photography packages include selected retouched images and all original photos for many session types. Exact inclusions are shown on the booking page.

5. Q: How do I reserve a session?  
   A: Choose a real available date/time on book.pashynskaphoto.com and pay the deposit to reserve the spot.

### First Google Post

Create an Update/Post:

Title/first line:
`Summer Mountain Mini Sessions — Calgary / Canmore-style family photos`

Body:
`Limited summer mountain mini sessions are now available for June 20 and July 11. 30-minute session, 20 retouched photos, all original photos included, short video included, and a styled mountain setup. Book online while spots are available.`

CTA: `Book`

URL:
`https://book.pashynskaphoto.com/?utm_source=google&utm_medium=organic&utm_campaign=gbp_post&utm_content=mountain_mini_summer_2026`

---

## 2) Wfolio — safe manual checks only

Open: https://wfolio.com/my/site

Do not redesign. Do not paste large custom CSS unless explicitly approved.

### Check menu/CTA links

Make sure public CTAs that say Book / Check availability / Reserve route to:
`https://book.pashynskaphoto.com/`

Use UTM when Wfolio allows external URL:
`https://book.pashynskaphoto.com/?utm_source=wfolio&utm_medium=portfolio&utm_campaign=wfolio_cta&utm_content=<page_or_banner>`

### Redirect/manual URL issues to check

Known public mismatch to fix if Wfolio UI allows:

- Old/broken `/services-pricing` should redirect or be replaced by `/services`.
- If a menu item still points to `/services-pricing`, change it to `/services`.
- If a menu item points to `/book-a-session` but real page is `/contacts`, either change menu URL to `/contacts` or make it external to `https://book.pashynskaphoto.com/`.

### SEO/schema warning

If you open Settings → Integration/custom code and see JSON-LD schema with old prices, do not free-edit randomly. Copy current block first. Prices should match:

- Regular Session: $290 + GST
- Mini Sessions: $180–230 + GST
- Engagement: $290 + GST
- Small Wedding: $580 + GST
- Full Wedding: $1160 + GST

---

## 3) Meta Ads Manager — cleanup review, do not mass delete

Open: https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=902493111302920

### Keep active

- `Mountain Mini Booking Site — June 2026`
- Ad: `Mountain Mini Flyer — Booking CTA`

### Confirm paused duplicate

These should be PAUSED:

- Campaign: `A/B Mountain Mini Booking Site — Hermes 2026-06`
- Adset: `Calgary broad parents/couples → booking site — same budget`
- Ad: `Mountain Mini Sessions — Existing IG creative + Hermes targeting A/B`

### Mark old boosted posts for review

Do not delete. For old active Instagram Post boosts, inspect last 7/14 days:

Pause candidates if all are true:

- no current offer / not related to current booking push;
- no clear booking attribution;
- cost per messaging conversation is worse than the current winners;
- Iryna does not want to manually DM follow up.

Keep 1–2 best DM ads as baseline if they produce cheap real conversations.

---

## 4) Instagram/Facebook profile links

Instagram profile website should ideally be:
`https://book.pashynskaphoto.com/?utm_source=instagram&utm_medium=bio&utm_campaign=profile_link`

Facebook Page website/CTA should ideally route to:
- page website: `https://pashynskaphoto.com/`
- Book Now CTA: `https://book.pashynskaphoto.com/?utm_source=facebook&utm_medium=page&utm_campaign=book_now_cta`

Do not change bio text if unsure; only fix stale links.

---

## 5) Report back after Comet run

Return a short report with:

- Google Profile: website changed? appointment link changed? services/products added? post published?
- Wfolio: any menu/CTA links changed?
- Meta: duplicate confirmed paused? any old boosts paused?
- Instagram/Facebook: profile links changed?
- Screenshots or copied final URLs if possible.
