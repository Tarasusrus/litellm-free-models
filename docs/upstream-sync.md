# Синк с upstream

Форк — [natorus87/litellm-free-models](https://github.com/natorus87/litellm-free-models).
Правила разделения кода описаны в
[`docs/adr/0001-fork-conventions.md`](adr/0001-fork-conventions.md); здесь —
только процедура синка.

## Когда синкать

- Раз в 1–2 недели, или когда `sync-models.yml` в upstream добавил новых
  провайдеров/модели, которые хочется получить.
- Перед началом заметной работы над `fork/` — чтобы не решать конфликт
  дважды (сначала в своей ветке, потом при синке в `main`).
- Не обязательно синкать на каждый коммит upstream — форк живёт с задержкой,
  это нормально: merge, а не rebase, догоняет историю целиком.

## Команда

```bash
make sync-upstream
```

Делает `git fetch upstream` + `git merge upstream/main` (merge, не rebase —
история форка публичная, переписывать её нельзя). При конфликте останавливается
и печатает, где искать причину.

## После синка — обязательно

```bash
make test
make render-config-dry
```

Мержа без конфликтов недостаточно: `render-config-dry` ловит совместимость на
уровне данных (например, upstream переименовал алиас, на который ссылается
`fork/models.yaml`), а не только на уровне текста файла.

## Ожидаемые точки конфликта

Список — из ADR §2, здесь как чек-лист при разборе конфликта:

| Файл | Что там наше | Если конфликт |
|---|---|---|
| `docker-compose.yaml` | `proxy`/`settings-ui` сервисы, `entrypoint: fork/docker-entrypoint.sh` | оставить наш хук, взять апстримные правки образа/портов рядом |
| `Makefile` | `render-config*`/`check-config` зовут `fork/render.py`; `sync-upstream` | оставить наши targets, взять новые apstримные targets как есть |
| `onboard.py` | render-шаг зовёт `fork/render.py`; `GEMINI_API_KEY` обязателен | сохранить вызов `fork/render.py` |
| `find-shared-models.py` | `--write-docs` → `docs/upstream-README.md`; `is_paid_vendor_model` спрашивает `fork/discovery.py`; stale-check мержит `fork/models.yaml` | сохранить три наших хука, остальное — апстримное |
| `.github/workflows/ci.yml` | ставит `requirements-dev.txt`, рендерит через `fork/render.py`, drift-check `docs/upstream-README.md` | сохранить эти три шага |
| `pyproject.toml` | per-file ruff ignore для `find-shared-models.py` (`UP038`) | сохранить строку игнора |
| `AGENTS.md` | только сгенерированная таблица моделей | не редактировать руками — перегенерировать `find-shared-models.py --write-docs` после мержа |

`config.template.yaml`, `render-config.py`, `providers_config.py` и всё
остальное, что ADR не перечисляет как точку контакта, — байт-в-байт апстримные.
Конфликт там означает, что кто-то отредактировал их вручную в форке; это
самому нарушение ADR, а не ожидаемый конфликт — искать и убирать ручную правку,
а не мержить её.

## Проверка после мержа без конфликтов

```bash
git diff upstream/main -- config.template.yaml render-config.py providers_config.py
```

Пусто — форк не разошёлся с апстримом в файлах, которые обязаны быть идентичны.
Не пусто — искать, где и почему появилась ручная правка (см. ADR §1).

## Новый провайдер в апстриме

`tests/test_standard_route.py::test_every_provider_has_a_documented_priority`
красный после синка — значит upstream добавил провайдера, а
`fork/standard.py::PROVIDER_PRIORITY` про него не знает. Присвоить место в
очереди (ADR §3, таблица приоритетов) и в этом файле, и в таблице ADR —
руками, порядок задаёт оператор, тест только напоминает не забыть.

## Каталог моделей: `sync-models.yml`

В upstream есть `.github/workflows/sync-models.yml` — еженедельный джоб,
который прогоняет `find-shared-models.py --apply` по секретам `SYNC_*` и
открывает PR с найденными моделями. Файл байт-в-байт апстримный (ADR не
перечисляет его как точку контакта) — трогать поведение самого джоба через
синк, а не ручной правкой.

**В этом форке файл держим, но с закрытым `schedule`-триггером** — на личном
форке `SYNC_*` секреты не заведены, а без них джоб падает уже на шаге сборки
`.env` (`Too few provider keys configured`, см. тело workflow). Еженедельный
красный ран без действия с ним — просто шум в списке Actions. Оставлен
`workflow_dispatch`: синк каталога всё ещё можно прогнать вручную, когда
секреты появятся или для разового ручного апдейта.

Если в форке заведут secrets `SYNC_*` — вернуть `schedule` обратно, раскомментировав
блок в `.github/workflows/sync-models.yml`.
