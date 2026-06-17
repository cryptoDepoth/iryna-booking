# Square Booking UX analysis → ideas for Pashynska booking site

Source analyzed:

```text
https://book.squareup.com/appointments/0vukmzkmvt6mu2/location/AE37PDW88NPCZ/availability
```

Compared against:

```text
https://book.pashynskaphoto.com/?utm_source=pinterest&utm_medium=social&utm_campaign=pinterest_pins&utm_content=canoe_mini
```

## Executive summary

Square is not visually beautiful, but it is very strong in **booking certainty**:

- clear service selection;
- clear calendar navigation;
- clear next available date;
- visible appointment summary;
- visible hold timer;
- visible cancellation policy;
- visible total/tax/due breakdown;
- separate first/last name fields;
- optional appointment note;
- timezone notice.

Our Pashynska booking site is warmer and more branded, and the new deep-links are better for ads/social/GBP/Pinterest. But Square has several conversion-safety patterns we should copy in a Pashynska-style design.

The goal is NOT to make the site look like Square. The goal is to take the mechanics that reduce confusion and abandoned bookings.

---

## What Square does well

## 1. Appointment summary is always visible

Square keeps a side panel:

```text
Appointment summary
Headshot and Portrait Session - In-Studio
199.00 CA$ ・ 1 hr
```

After selecting a time, it shows:

```text
Sunday, June 7
14:30 – 15:30
Est. due at appointment: 208.95 CA$
Subtotal: 199.00 CA$
Taxes: 9.95 CA$
Total: 208.95 CA$
Due today: 0.00 CA$
Due at appointment: 208.95 CA$
```

### Why this matters

Client always knows:

- what they selected;
- what date/time;
- how much it costs;
- what is due today;
- what is due later.

### Pashynska opportunity

Our drawer currently shows date, location, slots and form. It should also show a compact sticky summary:

```text
Your session
Canoe Mini Session
Sat, July 4 · Carburn Park
20 min · 15 edited photos
Deposit today: $110.25 + GST
Total: $220.50 + GST
```

Priority: **High**

---

## 2. Hold timer is visible immediately at checkout

Square shows:

```text
Appointment held for 9:54
```

### Why this matters

Creates urgency and reassurance:

- “this slot is temporarily mine”; 
- “I should finish booking now”; 
- “someone else cannot grab it while I type.”

### Pashynska opportunity

Our copy says slot is held for 15 minutes, but in the first drawer stage the timer is not visually prominent.

Add timer after a time is selected or after hold creation:

```text
We’re holding 13:00 for you · 14:58 left
```

Make it warm, not scary:

```text
Your time is safely held while you finish ✨ 14:58
```

Priority: **High**

---

## 3. Square separates date/time selection from contact checkout

Flow observed:

1. Services list.
2. Service detail.
3. Book.
4. Calendar/week availability.
5. Select time.
6. Checkout contact info.

### Why this matters

Client makes one decision at a time.

Our Pashynska flow is faster, but drawer contains:

- slots;
- contact form;
- payment CTA;
- info in same visual panel.

This is good for speed, but can feel dense on mobile.

### Pashynska opportunity

Keep current fast drawer, but visually split it into steps:

```text
1. Choose time
2. Your details
3. Deposit
```

After selecting time, highlight step 2.

Priority: **Medium-high**

---

## 4. Calendar has week navigation + “Go to next available”

Square shows disabled dates and says:

```text
No availability until Sunday, 7 June.
Go to next available
```

### Why this matters

If a date has no slots, the client doesn’t feel stuck.

### Pashynska opportunity

For fixed mini sessions, this is less critical, because cards already show session dates. But for Family/Maternity evergreen pages or future flexible sessions, add:

```text
Next available mini session
Next available family session
Next available maternity session
```

And if current event is sold out:

```text
This date is fully booked — view the next Canoe Mini Session
```

Priority: **Medium** now, **High** when evergreen booking expands.

---

## 5. Availability grouped by Morning / Afternoon / Evening

Square shows:

```text
Morning: No availability
Afternoon: 14:30
Evening: No availability
```

### Why this matters

For many slots, grouping reduces scanning effort.

### Pashynska opportunity

Our mini sessions only have a few slots, so not urgent. But if a session has 8–12 slots, group them:

```text
Afternoon
13:00 13:30 14:00 14:30

Golden hour
17:30 18:00 18:30
```

For photography this could be even better than Square:

```text
Best light ✨
18:00 18:30 19:00
```

Priority: **Medium**

---

## 6. Cancellation policy shown before booking

Square shows a cancellation policy block before the final button:

```text
Please cancel or reschedule before 14:30 on Sunday, June 7.
See full policy
```

### Why this matters

Reduces disputes and gives confidence.

### Pashynska opportunity

Add a collapsed policy block before payment:

```text
Cancellation & reschedule
Deposit reserves your time. If weather is unsafe, we reschedule. Final balance is due on session day.
```

Maybe make it more photography-specific:

```text
Weather note: if rain/wind makes the session impossible, we’ll reschedule together.
```

Priority: **High**

---

## 7. Appointment note field

Square has:

```text
Appointment note → Add
```

### Why this matters

Useful for:

- number of kids;
- maternity week;
- family names;
- special concerns;
- dog/pet;
- outfit help;
- accessibility.

### Pashynska opportunity

Add optional textarea:

```text
Anything Iryna should know? Optional
Examples: kids’ ages, maternity week, dog, outfit question, preferred vibe.
```

For mini sessions, keep optional and collapsed by default so it doesn’t slow booking.

Priority: **Medium-high**

---

## 8. Phone country code selector and phone consent text

Square has:

```text
🇨🇦 +1
Phone number
```

And legal SMS consent text.

### Why this matters

Calgary clients mostly use +1, but country code makes phone input clearer. Consent text protects automated SMS usage.

### Pashynska opportunity

At minimum:

- default phone placeholder: `403-555-1234`;
- keep +1 assumption;
- add small note:

```text
We’ll use this only for booking updates and reminders.
```

If adding SMS automation later, add stronger consent language.

Priority: **Medium**

---

## 9. Separate First name / Last name

Square uses:

```text
First name
Last name
```

Our site uses:

```text
Full name
```

### Why this matters

Separate fields are cleaner for CRM/email personalization.

### Pashynska recommendation

Do NOT change immediately. `Full name` is lower friction.

If CRM/email automation becomes more advanced, split later or parse automatically.

Priority: **Low**

---

## 10. Sign-in option

Square has optional sign-in.

### Why this matters

Good for repeat clients, but not needed for us now.

### Pashynska recommendation

Do NOT add login. It adds friction and complexity.

Priority: **Do not implement now**

---

## Things Square does worse than Pashynska

## 1. Generic / not emotional

Square feels transactional. Pashynska’s site feels branded and warm.

Keep our emotional visuals, reviews, and “how it works.”

## 2. Too many steps for mini sessions

Square makes the client go service → detail → calendar → checkout.

For social/GBP/Pinterest direct links, our auto-open drawer is better.

## 3. Weak photography-specific selling

Square does not show:

- photos strongly in the flow;
- outfit guidance;
- “best light”; 
- what’s included visually;
- family/maternity emotional reassurance.

Our site should stay more photography-specific.

---

## Recommended implementation priorities

## P0 — implement first

### 1. Sticky mini appointment summary inside drawer

Add to drawer:

```text
Your session
[Title]
[Date] · [Location]
[Duration]
Deposit today: $X + GST
Total: $Y + GST
```

Why: biggest clarity improvement.

### 2. Cancellation / weather / reschedule block

Add collapsed block before payment:

```text
Cancellation & weather
Your deposit reserves the time. If weather makes the session unsafe, we’ll reschedule. Final balance is due on session day.
```

Why: reduces fear before payment.

### 3. Optional note field

Add optional field:

```text
Anything Iryna should know? Optional
```

Why: captures useful context without full questionnaires.

## P1 — implement second

### 4. Visible hold timer

When slot is held:

```text
Your time is held for 14:58
```

If timer expires:

```text
This hold expired — please choose a time again.
```

### 5. Better sold-out / next event routing

If a deep-linked event is sold out or past:

```text
This date is fully booked. View next Canoe Mini Session.
```

This is especially important for evergreen Pinterest/GBP links.

### 6. Slot grouping by light/time

For many slots:

```text
Afternoon
Golden hour ✨
```

## P2 — later

### 7. Calendar week/month picker for flexible sessions

Useful when adding flexible family/maternity booking, not essential for fixed mini sessions.

### 8. Tax / due today / due later detailed breakdown

If payment flow grows beyond e-transfer deposit, show exact breakdown like Square.

### 9. Client account/login

Skip for now. Not worth complexity.

---

## Suggested Pashynska-style copy

### Summary block

```text
Your session
Canoe Mini Session
Sat, July 4 · Carburn Park
20 minutes · 15 edited photos + all originals
Deposit today: $110.25 + GST
Total: $220.50 + GST
```

### Hold timer

```text
Your time is safely held ✨ 14:58 left
```

### Cancellation/weather block

```text
Cancellation & weather
Your deposit reserves this time. If weather makes the session unsafe, we’ll reschedule together. Final balance is paid on session day.
```

### Note field

```text
Anything Iryna should know? Optional
Kids’ ages, maternity week, dog, outfit question, or preferred vibe.
```

### Sold-out fallback

```text
This date is fully booked, but there’s another Canoe Mini Session available.
View next date →
```

---

## Final recommendation

Do not copy Square’s full structure. Copy these mechanics:

1. visible appointment summary;
2. hold timer;
3. cancellation/weather clarity;
4. optional note;
5. next available fallback;
6. better price/due breakdown.

These are practical, low-risk improvements that can make Pashynska’s site feel more trustworthy while keeping the current warm branded design.
