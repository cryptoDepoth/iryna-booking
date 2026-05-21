# Анализ деплоя ассистента — 2026-05-10

## Проблема
Ассистент на сайте отвечал "необдуманно": говорил про прошедшие сессии (Blossom Mini Sessions, 3 мая) и игнорировал актуальные (Lilac Mini Sessions, 7 июня). Подозрение — к агенту не подключена модель из списка Hermes.

---

## Что оказалось на самом деле

### 1. Модель была подключена — но Z.ai упёрся в лимиты
Журналы Fly показали, что ассистент действительно обращался к Z.ai (`source: zai`), но:
- `glm-4.5-flash` → **ReadTimeout** (35 секунд, без ответа)
- `glm-4.5-air` → **429 Insufficient balance** (кончились кредиты на Z.ai)
- Остальные GLM-модели → **Unknown Model 400**

То есть модель была подключена, но платформа Z.ai перестала отвечать на продакшене.

### 2. Главная причина некорректных ответов — мусор в контексте
В `events.yaml` оставались старые сессии + тестовые события без фото:
- `blossom-may3` — дата 2026-05-03, уже прошла
- `eee-2026-05-17` и `x-2026-05-17` — тестовые записи без фото

Эти события попадали в контекст ассистента и на публичный `/events`, потому что фильтр был слишком мягким: `status in ("active", "upcoming", "completed")`.

---

## Выполненные исправления

### A. Фильтрация контекста (app.py + assistant_engine.py)
- Убрано `"completed"` из публичного списка.
- Добавлены строгие условия для публичных событий:
  - `status` ∈ ("active", "upcoming")
  - `not hidden`
  - `photos` заполнено (доказательство публичности)
  - `date >= today` (только будущие)
- Аналогичная фильтрация применена к контексту ассистента в `assistant_engine._event_lines()`.

### B. Поддержка OpenRouter (assistant_engine.py)
- Добавлена новая функция `_call_openrouter()`.
- Провайдер `openrouter` добавлен в основную цепочку `auto` **перед** Z.ai.
- Используемая модель: `google/gemini-2.5-flash-lite` — бесплатная, быстрая (~0.5–2 сек), отвечает на русском и английском.
- Тесты:
  - Локально: 26/26 тестов проходят.
  - Таймаут OpenRouter: 25 сек.
  - Заголовки `HTTP-Referer` и `X-Title` проставлены.

### C. Регрессионное тестирование (tests/test_booking_flow.py)
Добавлено 4 новых теста:
1. `test_public_events_exclude_past_and_hidden_sessions`
2. `test_assistant_event_context_excludes_past_active_sessions`
3. `test_assistant_chat_fallback_works_without_openai_key` — обновлён под `openrouter`
4. `test_assistant_chat_uses_openrouter_provider`

---

## Результаты деплоя

### Деплой 1 — fix: filter public assistant event context
- Commit: `342f47f`
- Летит — ОК. Но ассистент всё ещё отвечал fallback из-за таймаута Z.ai.

### Деплой 2 — feat: support OpenRouter for site assistant
- Commit: `317ea33`
- Секреты на Fly: `AI_PROVIDER=openrouter`, `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `ASSISTANT_OPENROUTER_TIMEOUT=25`
- Машина стартовала без ошибок, smoke-check — OK.

### Live-проверка (https://iryna-booking.fly.dev)
| Проверка | Результат |
|----------|-----------|
| `GET /` | 200, 108 KB |
| `GET /events` | Только `lilac-jun7` (1 событие) |
| `POST /assistant/chat` EN | source=`openrouter`, latency=911 мс, ответ про Lilac Mini Sessions, депозит $100 |
| `POST /assistant/chat` RU | source=`openrouter`, latency=973 мс, корректный ответ на русском про сиреневую сессию |
| Z.ai fallback | Не нужен — OpenRouter отвечает стабильно |

---

## Что изменилось в конфигурации

| Переменная | Было | Стало |
|------------|------|-------|
| `AI_PROVIDER` (Fly secret) | `zai` | `openrouter` |
| `ZAI_API_KEY` | Настроен (неиспользуем) | Оставлен на всякий случай |
| `OPENROUTER_API_KEY` | Отсутствовал | Добавлен (из локального `.hermes/.env`) |
| `OPENROUTER_MODEL` | Отсутствовал | `google/gemini-2.5-flash-lite` |
| `ASSISTANT_OPENROUTER_TIMEOUT` | Отсутствовал | `25` |

---

## Вывод

1. **Проблема была двойная**: мусорный контекст из старых/тестовых событий + отказ Z.ai по балансу/таймаутам.
2. **Контекст очищен** — ассистент теперь видит только `lilac-jun7`.
3. **Модель переключена на OpenRouter** — стабильно, быстро, бесплатно.
4. **Тестовое покрытие расширено** — 26 тестов, все проходят.
5. **Деплой успешен** — ассистент на сайте теперь отвечает корректно и на английском, и на русском.
