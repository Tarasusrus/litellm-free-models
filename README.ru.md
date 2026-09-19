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
- **Маршрут `tools`** — та же цепочка, суженная до моделей, которые живой
  проверкой прошли вызов инструмента в два шага. Для агентов с MCP-серверами:
  `standard` вызов инструментов не гарантирует.
  См. [Вызов инструментов / MCP-агенты](#вызов-инструментов--mcp-агенты).
- **Запуск одной командой** — `docker compose up -d` рендерит конфиг внутри
  контейнера прокси. Никакого `make render-config` на хосте.
- **Страница настроек** — локальная страница для ключей провайдеров:
  статус, живая проверка, «Применить» пишет `.env` и перезапускает прокси.
  См. [Страница настроек](#страница-настроек).
- **Документ для клиента** — [docs/USAGE.md](docs/USAGE.md) (EN): всё,
  что нужно клиенту (или агенту, который его пишет), включая структурированный
  вывод и его ограничения.
- **Апстрим остаётся апстримом** — код форка живёт в [`fork/`](fork/) и
  дорабатывает вывод апстримного рендера; шаблон и рендер побайтно совпадают
  с апстримом. Как и почему: [docs/adr/0001-fork-conventions.md](docs/adr/0001-fork-conventions.md).

## Быстрый старт

```bash
git clone https://github.com/Tarasusrus/litellm-free-models.git && cd litellm-free-models
cp .env.example .env      # задать LITELLM_MASTER_KEY + ключи провайдеров
docker compose up -d
```

Проверка: `curl -sf localhost:4444/health/readiness` → `{"status":"healthy",…}`.

Наружу опубликованы только порт прокси (`4444`, меняется через `LITELLM_PORT`
в `.env`) и страница настроек (`127.0.0.1:4445`); Postgres и Redis остаются
внутри compose-сети. Провайдеры с пустым
ключом просто не попадают в конфиг. Рестарт, обновление, остановка —
[docs/run.md](docs/run.md).

`POSTGRES_PASSWORD` и `REDIS_PASSWORD` — внутренние пароли только для
compose-сети, руками задавать не нужно: первый `docker compose up`
генерирует их в `.env`, повторные запуски не трогают. Оставили пустым и
`LITELLM_MASTER_KEY` — тоже сгенерируется, один раз, значение видно в
`docker compose logs env-init`.

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
print(reply.choices[0].message.content)
```

Все апстримные алиасы (`gpt-oss-120b`, `llama-3.3-70b-instruct`, эмбеддинги,
аудио, …) по-прежнему доступны по имени — матрица моделей в
[апстримном README](docs/upstream-README.md#-models). Формат запроса и
ответа, структурированный вывод, вызов инструментов, лимиты и коды ошибок — [docs/USAGE.md](docs/USAGE.md).

## Вызов инструментов / MCP-агенты

Агент, который вызывает инструменты (свои функции или MCP-сервер), шлёт
`model: "tools"` с `tools` в обычном OpenAI-формате. За именем — те же
провайдеры, что в `standard`, в том же порядке, но только модели, которые
при живой проверке вернули корректный `tool_calls`; список проверенных с
датой — `TOOL_CALLING_VERIFIED` в [`fork/tools_route.py`](fork/tools_route.py).
`standard` держит всех провайдеров, включая тех, кто молча выкидывает
описания инструментов и отвечает прозой, — для агентов он не подходит.

```python
first = client.chat.completions.create(model="tools", messages=messages, tools=tools)
call = first.choices[0].message.tool_calls[0]           # шаг 1: модель просит инструмент
messages += [first.choices[0].message,
             {"role": "tool", "tool_call_id": call.id, "content": run(call)}]
final = client.chat.completions.create(model="tools", messages=messages, tools=tools)  # шаг 2
```

Полный пример в два шага, MCP-клиент, который отдаёт список инструментов
сервера в `tools`, и как перепроверить бэкенд:
[docs/USAGE.md → Tool calling / MCP agents](docs/USAGE.md#tool-calling--mcp-agents-model-tools) (EN).

## Страница настроек

`docker compose up -d` поднимает и `settings-ui` — страницу на
`http://localhost:4445` (порт меняется через `SETTINGS_UI_PORT` в `.env`;
слушает только loopback). Вход по `LITELLM_MASTER_KEY`.

Что на ней — по строке на провайдера из `providers_config.py`, в порядке
приоритета цепочки `standard`:

- ключ маской и его состояние (пусто / есть / пример из `.env.example` /
  тир по умолчанию);
- **Check** — живой запрос каталога моделей с сохранённым ключом (те же
  запросы, что у `find-shared-models.py`): сколько моделей или какая ошибка.
  Результат кэшируется, пока ключ не изменится или сервис не перезапустится;
- **where to get** — консоль провайдера.

**Apply** пишет изменённые ключи в `.env` (атомарная замена файла, права
0600, остальные строки не тронуты), перезапускает прокси через Docker-сокет,
ждёт готовности и показывает цепочки `standard` и `tools`, которые прокси
отрендерил.
В ответах API ключи только маской, в логи не попадают. Страница правит только
переменные провайдеров; мастер-ключ и пароли остаются в `.env` руками.

Сервису смонтированы репозиторий на запись (ради замены `.env`) и
Docker-сокет только на чтение; сокет всё равно позволяет перезапустить любой
контейнер на хосте — поэтому страница висит на loopback и закрыта
мастер-ключом.

## Добавить ключ провайдера руками

1. Получить бесплатный ключ у провайдера (ссылки и лимиты: [апстримный README →
   Providers](docs/upstream-README.md#-providers)).
2. Вписать в `.env` (имена переменных — в `.env.example`).
3. `docker compose restart litellm-proxy` — прокси читает ключи из `.env` при
   каждом старте, рендерит конфиг заново, chat-модели провайдера встают в
   цепочку `standard` на своё место (а в `tools` — после попадания в его
   allowlist).

Текущие цепочки печатаются в стартовом логе прокси
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
