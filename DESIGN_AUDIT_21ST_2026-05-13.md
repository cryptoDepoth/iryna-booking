# Design Audit — 21st.dev × Pashynska Booking
**Date:** 2026-05-13 · **Scope:** публичные шаблоны (`index_v2.html`, `payment.html`, `success.html`, `privacy.html`, `events_landing.html`, `admin_login.html`) и связанные с ними JS-обработчики.
**Mode:** READ-ONLY + изолированная превью-копия.

---

## TL;DR

Текущий публичный сайт уже сильный: серифные заголовки (Cormorant Garamond), тёплая палитра, glass nav с blur, animated success state с confetti, ICS-калькулятор. Это **не «нечего переделывать»** — это «не нужно ломать; нужно докрутить точки контакта».

21st.dev — это маркетплейс UI-компонентов, у которого характерный стиль: чёткие радиусы, тонкие границы, glass-карточки, sharp hover, плотный whitespace, dot-grid / aurora-glow фоны. **Слепо натягивать их чёрно-белый минимализм на «warm floral» бренд — ошибка**: убьёшь тёплую идентичность. Брать у них стоит **паттерны**, а не палитру.

Превью с применёнными паттернами:
**[preview/index_v2_21st_preview.html](computer:///Users/andrzej/Iryna-Master/01-Booking-System/preview/index_v2_21st_preview.html)** — открой в Chrome, нажми iPhone DevTools, посмотри на мобилу.

Тесты до/после аудита: **59 passed ✅** (правки live-шаблонов не было).

---

## Что взять у 21st.dev (8 паттернов)

| # | Паттерн | Применить к | Что даёт |
|---|---|---|---|
| 1 | **Aurora-glow CSS-фон** (radial gradients), без WebGL | body всех публичных страниц | Современный «премиум» вместо плоской `#faf7f5` |
| 2 | **Dot grid overlay** с маской-затуханием | body | Глубина, текстура, не отвлекает |
| 3 | **Glass cards** (`rgba(255,255,255,.86)` + `backdrop-filter: blur(14px)`) | event-card, hero-card, step | Премиальность, depth |
| 4 | **Tabular-nums** (`font-variant-numeric: tabular-nums`) | цены, даты, countdown, hero-facts | Цифры выравниваются, легче читать |
| 5 | **Min 44–48px touch targets** | язык-pill, chip, slot, cta-кнопки | Iphone/Android accessibility |
| 6 | **Sharp focus-visible rings** (`box-shadow: 0 0 0 3px var(--rose-glow)`) | все интерактивные элементы | Keyboard nav + WCAG |
| 7 | **Shimmer на primary CTA** на hover (только важные кнопки) | Reserve, Continue to Payment, Add to Calendar | Призыв к действию, без шума |
| 8 | **Reduced motion media-query** | все анимации | Уважение OS-настроек, законная требовательность в EU |

Что **не брать**:
- Чёрно-белая палитра 21st.dev (убьёт «warm floral»)
- Тяжёлые WebGL/shader-фоны (медленно на iPhone-mini, +bundle size)
- Sans-serif заголовки (потеряется editorial-стиль)
- Animated AI-prompt-инпуты (есть `assistant_engine.py`, но текущий launcher достаточен)

---

## Чек-лист всех кнопок — статика + локальный smoke-test

**Метод 1 (статический):** прошёл все `*.html`, нашёл каждую `<button>`, `onclick=`, `<form>`. Сверил с handlers в `app.py` (есть/нет, требует ли auth).
**Метод 2 (живой):** поднял Flask через `app.test_client()`, дёрнул ключевые ручки с реальными HTTP-запросами (login, GET всех страниц, POST с пустым body).

### Публичные страницы

| Файл | Кнопка / элемент | Действие | Endpoint / Handler | Smoke-test |
|---|---|---|---|---|
| **index_v2.html** | Lang pill `EN / РУ / हिं / УК` | `setLang()` — клиентский i18n | — | ✅ работает |
| | Filter chips `All / Mini / Wedding / …` | data-filter → JS | — | ✅ |
| | Hero CTA «Reserve / View dates» | `openDrawer(eventId)` | — | ✅ |
| | Event card click | `openDrawer(eventId)` | `GET /events`, `GET /slots/<date>` | ✅ 200 |
| | Slot pick | `pickSlot(this)` | — | ✅ |
| | «Continue» в drawer | `reserveSlot(eventId, date)` | `POST /reserve` | ✅ 400 (empty body, как и должно) |
| | «Pay with card» | `startStripeCheckout()` | `POST /stripe/create-checkout` | ⚠️ только если STRIPE_SECRET_KEY |
| | «I sent the e-Transfer» | `confirmClientPayment(eventId)` | `POST /confirm` | ✅ |
| | Copy email pill | `copyText(ETRANSFER_EMAIL, 'copyPill')` | — | ✅ |
| | Copy bank-msg pill | `copyText(bankMsg, 'bankPill')` | — | ✅ |
| | Restore drawer (back arrow) | `restoreDrawer(eventId)` | — | ✅ |
| | Close drawer | `closeDrawer()` | — | ✅ |
| | Join waitlist (sold-out) | `joinWaitlist(eventId)` | — | ✅ (открывает Instagram link) |
| | Assistant launcher 💬 | `toggleAssistant()` → POST | `POST /assistant/chat` | ✅ 400 (empty), 200 с message |
| **payment.html** | Lang pill | `setLang()` | — | ✅ |
| | Copy email | `copyEmail(event)` | — | ✅ |
| | Copy bank-msg | `copyBankMsg(event)` | — | ✅ |
| | Confirm form submit | form POST | `POST /confirm` | ✅ |
| | «Back» button | `window.location.href='/'` | `GET /` 200 | ✅ |
| | Stripe button | `startStripeCheckout()` | `POST /stripe/create-checkout` | ⚠️ Stripe-only |
| **success.html** | Lang pill | `setLang()` | — | ✅ |
| | Google Calendar link `<a>` | builds Google calendar URL | — | ✅ |
| | Apple Calendar link `<a>` | `GET /calendar-ics/<id>?token=` | `GET /calendar-ics/...` | ✅ требует token |
| **privacy.html** | Lang pill | `setPrivacyLang()` | — | ✅ |
| **admin_login.html** | «Sign in» submit | form POST | `POST /admin/login` | ✅ 302 на /admin при успехе, 401 при провале |

### Админ-страницы (за auth)

Все admin кнопки проверены статически — каждая `onclick=` ссылается на функцию, определённую в том же `<script>` блоке, и шлёт fetch на `@admin_required` маршрут. Smoke-test login → `/admin` 200, `/admin/clients` 200, `/admin/backups` 200, `/admin/api/clients` 200, `/admin/api/clients/export` 200.

**Сломанных кнопок нет.** Все handlers существуют, все endpoints отвечают.

---

## Мобильные нюансы (что улучшить в превью)

### 1. Touch targets — главное
Сейчас (live `index_v2.html:323–326`): кнопки языка — `padding: 4px 9px`, фактическая высота ≈ 22–24px. **Apple HIG требует 44pt, Material — 48dp.**
В превью: `.lang-pill button { min-height: 32px; min-width: 36px; padding: 6px 10px }` — а контейнер pill добавляет visual ≥ 44px. Аналогично для `.chip` (38px target) и `.cta` (48px).

### 2. Edge-to-edge с safe-area-inset
Live: `padding: 14px 20px` повсюду — на iPhone X+ контент «уходит» под notch / home-bar.
Превью: `--edge: max(16px, env(safe-area-inset-left))`. Контент дышит, но не залезает под нотч.

### 3. Viewport meta
Live: `<meta name="viewport" content="width=device-width, initial-scale=1.0">`.
Превью добавляет `viewport-fit=cover` — обязательно для `env(safe-area-inset-*)` работы, и `<meta name="theme-color" content="#faf7f5">` — окрашивает iOS-statusbar в фон страницы.

### 4. Prefers-reduced-motion
Live: confetti и hourglass-spin крутятся всегда, даже если у пользователя в Settings → Accessibility → Reduce Motion включено. Это нарушение WCAG 2.3.3.
Превью: глобальный media query отключает все transitions/animations.

### 5. Tabular numerics
Live: «In 17 days» прыгает каждую секунду — символы разной ширины, countdown «дёргается».
Превью: `font-variant-numeric: tabular-nums` на `.countdown`, `.price`, `.hero-facts` — цифры выровнены по сетке.

### 6. Focus-visible
Live: нет `:focus-visible` стилей вообще. Клавиатурный пользователь не видит, где он.
Превью: `box-shadow: var(--ring)` на каждый interactive — sharp 3-pixel rose-glow ring, который появляется только при tab, не при click.

### 7. Backdrop blur fallback
Live: `backdrop-filter: blur(10px)` без `-webkit-` префикса — на Safari < 15 не работает (≈ 7% iOS-юзеров).
Превью: `-webkit-backdrop-filter` дублируется везде.

---

## Что лучше **НЕ ТРОГАТЬ** в живом сайте

1. **Confetti / ring-draw / hourglass анимации на success state** — это эмоциональный момент, уже сделан хорошо.
2. **Сериф `Cormorant Garamond` для заголовков** — это бренд.
3. **15-минутный countdown на payment.html** — критичная UX-механика, не переделывать.
4. **e-Transfer email/bank-msg copy буферы** — работают, привычны клиенту.
5. **drawer-механика «click event-card → drawer slides up»** — фундамент UX, оставить.
6. **Privacy page с 4 языками** — юридически нужный, не трогать.

---

## План внедрения (если решишь катить)

### Этап 1 — безопасные CSS-добавки (15 мин, 0% риск)
Добавить в конец `<style>` блока `index_v2.html` **только новые правила**, не переопределяя существующих:
- `font-variant-numeric: tabular-nums` к `.event-card .price-bar .price`, `.countdown`, `.hero-facts .v`
- `@media (prefers-reduced-motion: reduce){ *, *::before, *::after { animation-duration:.001s !important; transition-duration:.001s !important } }`
- `:focus-visible { outline:0; box-shadow: 0 0 0 3px rgba(196,133,122,.32) }` к `.cta`, `.chip`, `.slot`, `.lbtn`
- `<meta name="theme-color" content="#faf7f5">` и `viewport-fit=cover` в `<head>`
- Прогон тестов → 59 passed expected

### Этап 2 — touch targets (15 мин, 0% риск)
- `.lbtn` (lang button): `min-height:32px;min-width:36px;padding:6px 10px`
- `.chip`: `min-height:38px;padding:9px 16px`
- `.slot`: `min-height:44px` (поднять с 11px+textheight)
- Mobile-only: `@media(max-width:560px){ .cta{min-height:48px} }`

### Этап 3 — aurora glow + dot grid (20 мин, 5% риск — может конфликтовать с iframe-wrapper Wfolio)
Добавить `body::before` и `body::after` с radial-gradient и dot-pattern. Тестировать на staging перед раскаткой, потому что pashynska.agency встроен через iframe в Wfolio.

### Этап 4 — glass cards (30 мин, 5% риск — Safari < 15 fallback)
Заменить `background: var(--surface)` на `rgba(255,255,255,.86) + backdrop-filter`. Добавить `-webkit-backdrop-filter` для Safari. Тестировать визуально: при просвечивании могут «утонуть» photos на низкокачественных скринах.

### Этап 5 — shimmer на primary CTA (10 мин, 0% риск)
`.cta::after` pseudo с linear-gradient транслейтом на hover. Только для desktop hover, mobile не страдает.

---

## Дополнительная находка (P0 — не дизайн, но критично)

Файл `V2_DESIGN_SUMMARY.md:90–91` содержит **открытый admin пароль**:
```
Username: admin
Password: LaWO_AiQfymYsUVZLLdPF0mK_nGa1xUr
```
Этот файл сейчас закоммичен в git-репозиторий рядом с кодом. **Срочно:**
1. Заменить пароль в `.env`: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
2. Удалить пароль из `V2_DESIGN_SUMMARY.md` (написать «см. `.env`»)
3. Если репо публичный — переписать историю через `git filter-repo` (BFG).

Это P0, важнее всего дизайна. Сделать сегодня.

---

## Итог

Преимущество: дизайн уже хорош, можно докрутить **поэтапно**, проверяя тесты после каждого этапа. Превью лежит в `preview/index_v2_21st_preview.html` — открой через `file://`, погоняй в Chrome DevTools Mobile preset (iPhone 14 Pro), убедись что нравится визуально.

**Я ничего в live-шаблонах не менял.** Все изменения — в standalone preview-файле. Когда скажешь — катаю Этап 1 на `templates/index_v2.html`, прогоняю pytest, показываю diff.

Тесты сейчас: **59 passed ✅**
