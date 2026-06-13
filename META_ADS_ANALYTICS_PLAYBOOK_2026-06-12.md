# 📈 Meta Ads + Analytics Playbook — Pashynska Booking
**Дата:** 2026-06-12 · **Автор:** Opus 4.8 (Cowork) · **Статус кода:** в ветке, тесты 397 green, НЕ задеплоено
**Для:** Andrzej + следующие агенты. Цель — чтобы реклама приводила клиентов, а каждый доллар спенда был виден в выручке.

> Этот файл — источник правды по рекламе/аналитике. Если ты следующий агент: читай TL;DR → «Активация» → «Backlog». Не переписывай работающее (см. guardrails внизу).

---

## TL;DR

Кампания тратила бюджет, но конверсий не было по двум причинам, **не связанным с UX сайта** (бронь/оплата сделаны хорошо):

1. **Хаос с Meta Pixel.** Главная страница брони `index_v2.html` слала пиксель `1806486840358828`, а рекламный кабинет, лендинги и аналитика — `1335137335347797`. Оптимизирующийся на `1335` кабинет **не видел ни одного события** с главной страницы. (Был и третий, мёртвый, дефолт `902…` в `app.py`.)
2. **Purchase не фаился никогда.** Событие `booking_confirmed → Purchase` было описано, но нигде не вызывалось. Meta не получала сигнал о покупке → не могла оптимизировать и не показывала ROAS. На `payment.html`/`success.html` пикселя не было вовсе — воронка рвалась при переходе.

**Что сделано в коде этой сессии** (тесты зелёные):
- Один пиксель на весь сайт — `META_PIXEL_ID` (=`1335137335347797`) инжектится во все шаблоны через context-processor. Дрейф ID больше невозможен.
- Пиксель добавлен на `payment.html` (+`InitiateCheckout`) и `success.html` (+`Purchase`). Воронка непрерывна.
- **Серверный Purchase через Conversions API (CAPI)** — фаит в момент подтверждения оплаты (авто-eTransfer, Stripe, ручное подтверждение админом), даже если клиент уже ушёл. PII хешируется (sha256). Браузерный и серверный Purchase делят `event_id` (`purchase.<booking_id>`) → Meta дедуплицирует. **No-op, пока не задан `META_CAPI_TOKEN`** — деплоить безопасно.
- Новые тесты: `tests/test_meta_pixel_and_capi.py` (7 шт.) — пиксель один, старый `1806` не вернётся, payload CAPI корректен, PII хешируется, Purchase фаит только на confirmed.
- Убран баг в незакоммиченной правке `index_v2` (Jinja в JS-литерале + двойной малформед `slot_selected`).

**3 шага активации (делает Andrzej, см. ниже): deploy → `META_CAPI_TOKEN` секрет → сменить URL объявления на deep-link + UTM.**

---

## 🚀 Активация (по порядку)

### Шаг 1 — Деплой кода
```bash
cd /Users/andrzej/Iryna-Master/01-Booking-System
python3 -m pytest -q                       # ждём ~397 passed
flyctl deploy --remote-only --yes -a iryna-booking
```
Это также уберёт прошедшие сессии из публичной сетки (фильтр `date >= today` уже в коде — на проде его не было).
Проверка после: `curl -s https://book.pashynskaphoto.com/ | grep -c "1335137335347797"` → должно быть >0, а `grep -c "1806486840358828"` → `0`.

### Шаг 2 — Включить серверный Purchase (CAPI)
1. Events Manager → выбрать пиксель **1335137335347797** → **Settings → Conversions API → Generate access token** (нужен System User token).
2. Залить секрет на Fly (НЕ в git):
   ```bash
   flyctl secrets set META_CAPI_TOKEN="EAAB...твой_токен" -a iryna-booking
   ```
3. Временно для проверки: `flyctl secrets set META_TEST_EVENT_CODE="TEST12345"` (код из Events Manager → **Test Events**). Сделай тестовую бронь+оплату → увидишь `Purchase` в Test Events. Затем **сними** код: `flyctl secrets unset META_TEST_EVENT_CODE`.

### Шаг 3 — Сменить URL объявления (это #1 рычаг конверсии)
Реклама должна вести не на голый `/`, а на **deep-link конкретной сессии + UTM** (drawer откроется сам, клиент сразу видит то, за чем пришёл):

```
https://book.pashynskaphoto.com/?event=<EVENT_ID>&utm_source=instagram&utm_medium=paid&utm_campaign=<КАМПАНИЯ>&utm_content=<AD_ID>
```
- `<EVENT_ID>` — точный id живой будущей сессии. Узнать: `curl -s https://book.pashynskaphoto.com/events` (поле `id`) или в админке. Пример из аудита: `mountain_mini`.
- Перед запуском в админке поставь нужной сессии **`featured: true`** — она станет hero для всего трафика.
- UTM + `fbclid` уже пишутся в бронь при `/reserve` → связка ad→бронь видна в `/admin/analytics`.

> Без шага 3 даже идеальный трекинг не спасёт: 86 показов → 0 броней в прошлый раз были из-за разрыва «обещание рекламы → лендинг».

---

## 🔭 Карта событий воронки (после деплоя)

| Действие клиента | Внутреннее | Meta-событие | Где фаит |
|---|---|---|---|
| Загрузка любой страницы | `page_view` | `PageView` | index_v2 / payment / success |
| Открыл карточку сессии | `drawer_open` | `ViewContent` | index_v2 |
| Выбрал слот | `slot_selected` | `ViewContent` | index_v2 |
| Начал заполнять форму | `form_started` | `Lead` | index_v2 |
| Нажал «Reserve» | `reserve_attempt` | `Lead` | index_v2 |
| Дошёл до оплаты | `payment_view` | `InitiateCheckout` | index_v2 + payment.html |
| «Я отправил e-Transfer» | `payment_sent_clicked` | `AddPaymentInfo` | index_v2 |
| **Оплата подтверждена** | `booking_confirmed` | **`Purchase`** | **сервер CAPI** + браузер на success.html (дедуп) |

**Оптимизируй кампанию на `Purchase`** (или, на старте при малом объёме, на `InitiateCheckout`/`Lead`, потом переключись на `Purchase`).

---

## 🎯 Ads Manager — корректная настройка

**Цель кампании:**
- Для броней на сайте → **Sales (Conversions)**, событие `Purchase` (после набора данных). На старте, пока Purchase < ~15/нед — **Leads** на `InitiateCheckout`.
- Параллельно отдельная кампания **Engagement → Messages** (Instagram DM) — она уже даёт диалоги (CTR ~6%), не трогать пока работает.

**Аудитория (Calgary):**
- Локация: Calgary +25–50 км. Возраст 25–40. Языки: EN/UK/RU.
- Интересы: фотография, mini sessions, motherhood/family, Banff/Rocky Mountains, локальные wedding/maternity.
- Через 2 недели: **Lookalike 1–3%** от тех, кто сделал `Purchase`/`Lead` (как соберётся пиксель).

**Креатив:**
- Реальные фото Ирины (не сток). 9:16 для Reels/Stories. 3–5 вариантов на ad set, ротация каждые ~2 недели (Frequency держать < 3).
- Текст: конкретика — дата, локация, что входит (15 фото за 48ч), цена/депозит. Чёткий CTA: «Book Now».

**Бюджет/ритм:** старт CBO $15–20/день на 2–3 ad set, lowest cost. Первые 24–48ч не трогать (обучение). KPI ниже.

**KPI:**
| Метрика | Хорошо | Отлично |
|---|---|---|
| CTR (link) | >2% | >4% |
| Cost / Lead (InitiateCheckout) | <$15 | <$8 |
| Cost / Purchase | окупается депозитом | <½ депозита |
| Frequency | <3 | <2 |
| Lead→Booking | >15% | >25% |

---

## 📊 Аналитика — петля контроля

- **Дашборд:** `https://book.pashynskaphoto.com/admin/analytics` (+ `/admin/analytics.csv` для выгрузки). Группирует воронку по `utm_campaign`/`utm_content` и связывает с бронями.
- **Раз в 48ч:** смотри по каждому `utm_campaign`: `drawer_open` rate (норма 20–40% от page_view), `reserve_attempt`, `booking_confirmed`. Считай **cost/booking = спенд кабинета ÷ confirmed-брони с этой кампании**.
- **Events Manager:** «Event Match Quality» для Purchase должен расти (мы шлём хешированные email/phone/имя + `fbc` из fbclid). Если красный — проверь, что fbclid доходит до брони.
- **Дисциплина UTM:** в КАЖДОМ объявлении уникальный `utm_content=<ad_id>` — иначе нельзя сравнить креативы.
- **Дедуп Purchase:** браузер+сервер шлют один `event_id` (`purchase.<id>`). Если в Events Manager видишь дубли Purchase — проверь, что `meta_event_id` доходит в success.html и `event_id` в CAPI совпадает.

---

## 🧱 Backlog для следующих агентов (по приоритету)

1. **Сделать шаги 1–3 «Активации»** — без них код мёртвый груз. (Andrzej)
2. **Проверить Event Match Quality** Purchase через 48ч после включения CAPI; добавить `client_ip_address`/`client_user_agent`/`fbp` в CAPI payload, если качество < «Good» (поднимет атрибуцию).
3. **`utm_term`** не пишется в бронь — добавить, если будешь резать по ключам.
4. **Lookalike + ретаргет** на `ViewContent`/`InitiateCheckout` без `Purchase` (брошенная корзина в рекламе), когда пиксель наберёт аудиторию.
5. **Второе abandoned-письмо** (план в `MULTIAGENT_AUDIT_2026-06-10.md`) — дешёвый возврат тёплых лидов.
6. **Единый Pixel и для landing-страниц через context** — `analytics.html` (если используется) и сторонние лендинги привести к `{{ meta_pixel_id }}`/`window.__META_PIXEL_ID`.

### Guardrails (в духе DOX / align-dev / guard-skills / canary)
- **Перед деплоем:** `pytest` зелёный (сейчас 397). Не деплой с красными тестами.
- **Не переписывай рабочее.** Бронь/оплата UX уже хороши — менять только при доказанной выгоде (Problem/Impact/Benefit/Risk).
- **Один пиксель.** Не хардкодь pixel id в шаблонах — только `{{ meta_pixel_id }}`. Тест `test_meta_pixel_and_capi.py` это сторожит.
- **Секреты не в git.** `META_CAPI_TOKEN`, Stripe, Telegram — только `fly secrets`/`.env` (gitignored).
- **CAPI идемпотентен** по `purchase.<booking_id>` — не плоди новые точки вызова Purchase, используй хук `_record_booking_funnel_event(..., "booking_confirmed", ...)`.
- **Не трогай прод-данные:** `events.yaml` на проде живёт в `/data` (репозиторный — только seed). События меняй через админку, не через файл.
- После деплоя — smoke: бронь end-to-end + `Purchase` в Test Events.

---

## 🗂️ Что менялось в коде (для ревью)
- `app.py` — `META_PIXEL_ID` дефолт → `1335…`; конфиг `META_CAPI_TOKEN`/`META_CAPI_API_VERSION`/`META_TEST_EVENT_CODE`; helpers `_sha256_norm`, `_meta_capi_purchase`; хук Purchase в `_record_booking_funnel_event`; `meta_pixel_id` в context-processor; `meta_event_id` в render payment/success.
- `templates/index_v2.html` — пиксель `1806`→`{{ meta_pixel_id }}`; откат багнутой WIP-правки трекинга.
- `templates/base_landing.html`, `static/js/analytics.js` — пиксель через переменную/`window.__META_PIXEL_ID`.
- `templates/payment.html`, `templates/success.html` — добавлен Pixel + событие (InitiateCheckout / дедуп-Purchase).
- `.env`, `.env.example` — задокументированы META-переменные.
- `tests/test_meta_pixel_and_capi.py` — новые тесты.
