# litellm-free-models (форк)

[English](README.md) · **Русский**

Один OpenAI-совместимый адрес поверх бесплатных LLM-провайдеров. Клиент
шлёт `model: "standard"`, прокси сам перебирает провайдеров по очереди,
пока кто-то не ответит.

## О форке

Это форк [natorus87/litellm-free-models](https://github.com/natorus87/litellm-free-models)
(v0.3.0, MIT). Апстрим делает основную работу: ведёт каталог бесплатных
провайдеров и моделей и рендерит из него конфиг
[LiteLLM](https://github.com/BerriAI/litellm)-прокси с балансировкой,
кулдаунами и цепочками фолбэков. Его README сохранён без изменений в
[docs/upstream-README.md](docs/upstream-README.md) — это справочник по
провайдерам, моделям, Kubernetes и multi-instance.

Что добавляет форк:

- **Маршрут `standard`** — все chat-деплойменты, для которых есть ключ, за
  одним именем модели, в фиксированном порядке провайдеров (Gemini → Groq →
  OpenRouter → Mistral → NVIDIA → … → анонимные тиры последними). Появился
  ключ в `.env` — его модели встают в цепочку при следующем старте, руками
  ничего править не нужно.
- **Запуск одной командой** — `docker compose up -d` рендерит конфиг внутри
  контейнера прокси. Никакого `make render-config` на хосте.
- **Документ для клиента** — [docs/USAGE.md](docs/USAGE.md) (EN): всё,
  что нужно клиенту (или агенту, который его пишет), включая структурированный
  вывод и его ограничения.
- **Апстрим остаётся апстримом** — код форка живёт в [`fork/`](fork/) и
  дорабатывает вывод апстримного рендера; шаблон и рендер побайтно совпадают
  с апстримом. Как и почему: [docs/adr/0001-fork-conventions.md](docs/adr/0001-fork-conventions.md).

## Быстрый старт

```bash
git clone https://github.com/Tarasusrus/litellm-free-models.git && cd litellm-free-models
cp .env.example .env      # задать LITELLM_MASTER_KEY, POSTGRES_PASSWORD, REDIS_PASSWORD + ключи провайдеров
docker compose up -d
```

Проверка: `curl -sf localhost:4444/health/readiness` → `{"status":"healthy",…}`.

Наружу опубликован только порт прокси (`4444`, меняется через `LITELLM_PORT`
в `.env`); Postgres и Redis остаются внутри compose-сети. Провайдеры с пустым
ключом просто не попадают в конфиг. Рестарт, обновление, остановка —
[docs/run.md](docs/run.md).

## Подключить клиента

- **Base URL:** `http://localhost:4444/v1`
- **API-ключ:** `LITELLM_MASTER_KEY` из `.env` (или виртуальный ключ, созданный через прокси)
- **Модель:** `standard`

```bash
curl -s localhost:4444/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"model": "standard", "messages": [{"role": "user", "content": "Скажи привет одним словом."}]}'
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:4444/v1", api_key="<LITELLM_MASTER_KEY>")
reply = client.chat.completions.create(
    model="standard",
    messages=[{"role": "user", "content": "Скажи привет одним словом."}],
)
print(reply.choices[0].message.content, "| ответил:", reply.model)
```

Все апстримные алиасы (`gpt-oss-120b`, `llama-3.3-70b-instruct`, эмбеддинги,
аудио, …) по-прежнему доступны по имени — матрица моделей в
[апстримном README](docs/upstream-README.md#-models). Формат запроса и
ответа, структурированный вывод, лимиты и коды ошибок — [docs/USAGE.md](docs/USAGE.md).

## Добавить ключ провайдера

1. Получить бесплатный ключ у провайдера (ссылки и лимиты: [апстримный README →
   Providers](docs/upstream-README.md#-providers)).
2. Вписать в `.env` (имена переменных — в `.env.example`).
3. `docker compose restart litellm-proxy` — конфиг рендерится заново при
   старте, chat-модели провайдера встают в цепочку `standard` на своё место.

Текущая цепочка печатается в стартовом логе прокси
(`docker compose logs litellm-proxy | grep -A200 "'standard' route"`) или
локально: `python3 fork/render.py --output /tmp/config.yaml`.

## Разработка

```bash
pip install -r requirements-dev.txt
make test     # юнит- и property-тесты (hypothesis)
make lint     # ruff
```

Правила форка — где живёт наш код, какие апстримные файлы задеты, как
синкаться: [docs/adr/0001-fork-conventions.md](docs/adr/0001-fork-conventions.md).

## Лицензия

MIT, как и у апстрима — [LICENSE](LICENSE).
