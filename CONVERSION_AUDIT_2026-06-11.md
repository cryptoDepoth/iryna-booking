# Conversion & Funnel Audit — Mountain Mini кампания
**Дата:** 2026-06-11 · **Агенты:** Funnel Analyst · CRO Engineer · Attribution Specialist · Performance Auditor
**Вопрос:** Website ad — 86 просмотров → 0 drawer opens → 0 броней. Почему?

---

## Диагноз: трекинг НЕ сломан. Реклама ведёт не туда.

### Как проверяли
- Код трекинга: `page_view` и `drawer_open` уходят **одним и тем же каналом** — `trackBookingEvent()` → `fetch('/track')` (index_v2.html:1182, 3590, 1854). Если записались 86 page_view, канал работает; 0 drawer_open = люди реально не открыли ни одной карточки. Это поведение, а не баг.
- Живой сайт (fetch 2026-06-11): hero = **«Golden Boho Moments», 14 июня**. Mountains Mini в hero нет.
- Прод `/events` JSON: Mountains Mini Session 2026-06-20 существует (7/10 мест, hidden:false), но идёт **последним из 6**, а перед ним в выдаче висят **две прошедшие сессии** — Lilac (2026-06-06) и Swing Blossom (2026-05-31) со status=active.

### Цепочка отказа
Клиент кликает рекламу «горы, Quarry Lake» → попадает на `/` → видит hero «бохо-пикник 14 июня» (не то, за чем пришёл) → листает сетку → первые карточки: прошедшие сессии → закрывает. 86 раз подряд. DM-реклама работает (CTR 6.08%, 7 диалогов), потому что там нет этого разрыва «обещание → лендинг».

---

## Ответы на 5 ключевых вопросов

**1. Почему 86 views = 0 drawer opens?** Ad-to-landing mismatch + мусор в сетке. Трекинг исправен (единый канал доставки событий, см. выше).

**2. Видит ли клиент сразу Mountain Mini?** Нет. Hero выбирается как «ближайший bookable» (`_select_hero_event`, app.py) → Golden Boho 14.06. Mountains — последняя карточка, ниже двух мёртвых.

**3. Можно ли deep-link?** ✅ **УЖЕ РАБОТАЕТ НА ПРОДЕ** — проверено живым запросом:
`https://book.pashynskaphoto.com/?event=mountain_mini` → og:title «Mountains Mini Session — Book Online», drawer автоматически открывается через 0.5с (index_v2.html:2609, `resolve_event_deeplink` app.py с алиасами mountain_mini / mountains_mini / mountainminisession + точный id). Авто-открытие drawer **фиксируется как drawer_open** в воронке (открытие идёт через тот же `openDrawer` → index_v2.html:1854).

**4. Что если Stripe не проходит?** Fallback есть: на payment-странице e-Transfer — основной способ, Stripe — secondary; при ошибке Stripe клиент остаётся на странице с e-Transfer (payment.html:427+). После вчерашних правок $0-чекаут тоже отбивается.

**5. Второе abandoned-касание?** Нет, только 2ч. План готов (MULTIAGENT_AUDIT_2026-06-10.md, NEXT №6): письмо через 18–28ч с проверкой «слот ещё свободен».

---

## TOP-5 фиксов

### 🔴 1. Сменить destination URL в Meta-объявлении (0 строк кода, 5 минут)
- **Сейчас:** реклама ведёт на голый `/` (точный URL сверить в Ads Manager — папка 02-Marketing-Ads из этой сессии недоступна).
- **Фикс:** `https://book.pashynskaphoto.com/?event=mountain_mini&utm_source=instagram&utm_medium=paid&utm_campaign=mountain_mini_jun20&utm_content=<ad_id>`
- Работает на ТЕКУЩЕМ проде, деплой не нужен. Ожидание: drawer_open rate с ~0% к норме 20–40% от page_view.

### 🔴 2. Задеплоить main (фильтр прошедших ивентов уже в коде)
- Локальный `_public_events_payload` (app.py) уже фильтрует `date >= today` — прод этой версии не имеет, поэтому показывает Lilac 06.06 и Swing 31.05.
- `flyctl deploy --remote-only --yes -a iryna-booking` (+ сначала `flyctl secrets set ADMIN_PASSWORD=...`, см. шаги выше). Заодно уедут приватные сессии и фиксы недели.
- **Тест после деплоя:** `curl -s https://book.pashynskaphoto.com/events | grep -c "2026-05-31"` → 0.

### 🟠 3. Featured = Mountains в админке (1 клик, без кода)
- В админке поставить Mountains Mini `featured: true` → hero для ВСЕГО трафика станет Mountains (логика `_select_hero_event`: featured+bookable первым). Golden Boho вернётся в hero после 20 июня автоматически (featured прошедшего не bookable).

### 🟡 4. Таймзона + отсечка прошедших слотов (код, ~1.5ч)
- Из MULTIAGENT_AUDIT NOW №1: сервер в UTC «уезжает в завтра» после ~18:00 Калгари — вечерний пик мобильного трафика ловит кривые даты; прошедшие слоты бронируемы в день съёмки. Прямо влияет на конверсию рекламы, крутящейся вечером.

### 🟡 5. Второе abandoned-письмо (~20ч, код готов в плане)
- MULTIAGENT_AUDIT NEXT №6: вторая попытка через 18–28ч, только если слот ещё свободен. Дешёвый возврат тёплых лидов (бросившие оплату уже оставили email).

### Что НЕ делать
- Не «чинить трекинг» — он работает; не переписывать лендинг; не трогать deep-link код (готов и проверен); не гнаться за perf (224KB HTML с lazy-картинками — не узкое место против релевантности).

---

## Порядок выполнения
1. **Сегодня, без деплоя:** фикс №1 (URL в Ads Manager) + №3 (featured в админке) → эффект уже на текущем проде.
2. **Сегодня, деплой:** №2 (`flyctl deploy`) — уберёт мёртвые карточки.
3. **Эта неделя:** №4 и №5 отдельными коммитами с тестами (план и диффы — в MULTIAGENT_AUDIT_2026-06-10.md).
4. **Контроль через 48ч:** /admin/analytics — drawer_open и booking_reserved по utm_campaign=mountain_mini_jun20; сравнить с DM-кампанией по cost/booking.

*Примечание по атрибуции: UTM + fbclid собираются на фронте и пишутся в booking при /reserve (app.py:4336) — связка ad → бронь уже есть в /admin/analytics. Для сверки ad-spend ↔ выручка достаточно дисциплины utm_campaign в каждом объявлении.*
