# Карта проекта LarkBot

Рабочий справочник по коду: что где лежит и как работает.
Составлен по итогам полного разбора проекта. Держать в актуальном состоянии
при заметных изменениях.

---

## 1. Что это

Telegram-бот приёма ошибок роботов на складах (GLP-C, SMALL-P3).
Сотрудник пишет в топик форум-группы, бот разбирает сообщение, пишет запись
в Supabase и отправляет карточку в Lark-группу через custom-bot webhook.

**Почему Telegram, а не Lark-события:** квота Open Platform (10 000 вызовов
в месяц) была исчерпана. Через webhook вывод идёт без квоты; квоту тратит
только загрузка фото в Lark (`im/v1/images`), и при её исчерпании фото
автоматически уходит ссылкой через Supabase Storage.

**Стек:** Python 3.11 (`Dockerfile`, `.python-version`), Flask (только
`/health`, `/p/<file>`, `/shift_stats`), long polling Telegram Bot API через
`requests` (без aiogram), Supabase через PostgREST (без SDK), Lark webhook.

---

## 2. Карта файлов

### Ядро

| Файл | Строк | Роль |
| --- | --- | --- |
| `telegram_bot.py` | 5203 | Точка входа. Long polling, маршрутизация топиков, команды, флоу фото/ошибки/статуса, Flask-роуты, лиз, фоновые потоки. |
| `telegram_api.py` | 559 | Тонкая обёртка Bot API: `call`, dry-run, flood-limit, send/edit/delete, файлы. |
| `sendToDataBase.py` | 1121 | PostgREST-доступ и главный писатель `send_to_data_base`, сменная статистика. |
| `supabase_storage.py` | 459 | Storage: бакеты, загрузка фото, подписанные ссылки, JSON-объекты. |

### Данные и сотрудники

| Файл | Роль |
| --- | --- |
| `telegram_store.py` | Привязка Telegram → `employees`: `/reg`, `/unreg`, `/whoami`, поиск имени с подсказками (rapidfuzz). |
| `warehouses.py` | Склады из `TELEGRAM_WAREHOUSES`, разбор аргумента-склада. |
| `error_parser.py` | Разбор формата `<тип>: <описание>. <номер робота>`. |
| `shift.py` | Смены: день 06:00–18:00, ночь 18:00–06:00, Europe/Warsaw. |
| `time_utils.py` | Устойчивый разбор ISO-времени из PostgREST. |

### Роботы и отчёты

| Файл | Роль |
| --- | --- |
| `robot_card.py` | Карточка робота: статус, история, ошибки, кэш, подсказки номеров. |
| `robot_status.py` | Направления offline/online, причины, смена статуса, карточка в Lark. |
| `robot_queue.py` | Автозакрытие очереди `robots_to_add` (интервал 30 мин). |
| `shift_report.py` | Отчёт по смене (карточка + текст), MTTR/простой, планировщик. |
| `digests.py` | Дайджест обслуживания в личку, раз в сутки после 09:00. |
| `analytics.py` | `/top`, `/downtime`, недельный отчёт, кандидаты на вывод. |

### Lark

| Файл | Роль |
| --- | --- |
| `lark_hooks.py` | Адреса вебхуков по складу и виду; `DEFAULT_TARGET_HOOK_URL`. |
| `lark_media.py` | Отправка карточек/текста/фото в webhook, HMAC-подпись, `upload_image`. |
| `lark_send.py` | Legacy-отправка через Open Platform (в боте недостижима). |
| `getToken.py` | tenant_access_token с кэшем и обновлением за 60 с. |
| `pending_photos.py` | Пересылка фото в Lark, фолбэк на ссылку Supabase. |

### Инфраструктура

| Файл | Роль |
| --- | --- |
| `bot_lease.py` | Лиз «единственного опрашивающего»: `bot_leases`, TTL 90 с. |
| `logging_config.py` | Ротация `logs/app.log` (5 МБ × 5), уровень из `LOG_LEVEL`. |
| `env_utils.py` | Безопасное чтение `env_int`/`env_bool` (мусор → значение по умолчанию). |
| `config.py` | Lark-креденшелы; при отсутствии — предупреждение, фото ссылкой. |
| `text_utils.py` | `truncate`, лимиты Telegram/Lark. |

### Production intake дерева решений

| Файл | Роль |
| --- | --- |
| `equipment_intake/types.py` | Модель: `NodeType`, `Option`, `Node`, `Step`, `DecisionTree`, валидация. |
| `equipment_intake/tree_config.py` | Дерево (12 узлов) и `load_tree` (конфиг/файл/JSON/URL). |
| `equipment_intake/engine.py` | Чистая логика: переходы, сброс ветки, breadcrumb, summary. |
| `equipment_intake/keyboards.py` | Отрисовка inline-клавиатуры. |
| `equipment_intake/flow.py` | Сессии, фото→дерево→номер→описание→финал, подтверждение, Supabase и Lark. |
| `equipment_intake/storage.py` | Общие добавленные варианты дерева в Supabase. |
| `equipment_intake/editor.py` | Редактор дерева из Telegram (`/tree`), доступен всем сотрудникам. |
| `equipment_intake/README.md` | Документация пакета. |

### Дерево решений: логика и данные

**Авторитетная логика** (задана заказчиком, живёт в `equipment_intake/tree_config.py`):

```
Object → Type → Device number → Description → Summary

Robot            → A42T C2 | A42T | K50H
Workstation      → Pick | Conveyor | Tally
Charging station → For A42T / C2 | For K50H
QR Code          → Shelf | Floor
```

Два уровня выбора кнопками, затем номер оборудования и описание — текстом.
Всего 8 узлов. Номер обязателен (бывают составные: `H108/1834`), описание —
свободный текст: это сбор сырых данных для будущих готовых вариантов.

**Данные и инструменты** (материал для расширения, по умолчанию не влияют):

| Файл | Роль |
| --- | --- |
| `data/sample_errors_2026-09-23_28.tsv` | Реальная выгрузка: 911 ошибок за 6 дней. |
| `data/ANALYSIS.md` | Разбор: распределения, выводы, размеры меню. |
| `tools/build_tree_from_errors.py` | Генератор дерева из выгрузки (идемпотентный). |
| `equipment_intake/tree.generated.json` | Дерево из данных: 56 узлов, 5 уровней. |

Дерево из данных доступно явно: `load_tree("generated")`. Когда в описаниях
накопится достаточно повторяющихся формулировок, из них делаются готовые
варианты и дерево расширяется — без правки UI.

### Тесты

| Файл | Чеков | Роль |
| --- | --- | --- |
| `tests/test_telegram_offline.py` | ~599 | Полный offline-набор бота, без сети (121 функция). |
| `tests/lark_sink.py` | — | Локальная заглушка Lark-вебхука на `127.0.0.1:8899`. |

### SQL

`sql/telegram_users.sql`, `sql/bot_leases.sql`, `sql/exception_photo.sql`
(колонка `photo_url`), `sql/exceptions_indexes.sql` (индексы + уникальный
`uniq_key` — на нём держится защита от дублей).

---

## 3. Поток сообщения (dispatch)

```
update → handle_update → _handle_update_inner
  1. callback_query            → handle_status_callback (dt: → ed: → rf: → em: → w: → st:)
  2. выучить имя топика        (forum_topic_created/edited)
  3. чёрный список топиков     → return
  4. сервисные сообщения       → return
  5. сообщение от бота         → return
  6. дедупликация              (chat:message_id / cb:callback_id)
  7. возраст сообщения         (> MESSAGE_MAX_AGE_SECONDS)
  8. маршрут + склад + топик-источник
  9. allowlist чата            (кроме /id, /help, /start)
 10. text                      → _handle_text_message → return
 11. photo                     → handle_photo        → return
 12. caption без фото          → разбор как текста
 13. прочее (стикер, голос…)   → тишина, только лог
```

Порядок важен: текст обрабатывается раньше фото, сервисные сообщения —
раньше всего остального.

---

## 4. Маршрутизация топиков

**Куда отвечать** (`_reply_thread`, по приоритету):
1. явно переданный `thread_id`;
2. контекст-менеджер `reply_thread(...)`;
3. топик текущего сообщения (`_origin_thread`);
4. последний известный маршрут чата (`_routes`).

Если отправка не удалась — фолбэк в `TELEGRAM_TOPIC_ID` (только для групп),
при этом кнопки сохраняются.

**Принимать ли сообщение** (`topic_allowed`):
- белый список `TELEGRAM_LISTEN_TOPICS` ограничивает monitored topics;
- если ничего не настроено — принимается всё (в логе громкое предупреждение);
- иначе совпадение по `TELEGRAM_TOPIC_ID` / ошибкам по складам / имени топика.

**Чёрный список** `TELEGRAM_IGNORE_TOPICS` — полное игнорирование, включая
команды. Полностью исключает выбранные топики из обработки.

---

## 5. Команды

`/reg`, `/whoami`, `/unreg`, `/stats`, `/robot`, `/digest`, `/top`,
`/downtime`, `/week`, `/topics`, `/offline`, `/online`, `/cancel`, `/id`,
`/help` (+ `/tree`). Ответ всегда уходит в топик-источник.

Полезно: команда распознаётся в русской раскладке (`ЙЦУКЕН → QWERTY`),
есть псевдонимы (`/req → /reg`), опечатки → подсказка «Did you mean…».
В меню Telegram публикуются 9 команд (остальные — по `/help`).
`/tree` намеренно не в меню и не в `/help` (доступен всем сотрудникам).

---

## 6. Фото-флоу

```
photo → get_file → download_file → images/tg_<file_unique_id><ext>
  ├ (a) фото → production дерево решений → return
  ├ (b) TELEGRAM_ERROR_MENU непусто   → меню типа ошибки → return
  ├ (c) PHOTO_ATTACH_ENABLED=0        → фото сразу в Lark → return
  └ (d) PHOTO_ATTACH_ENABLED=1        → привязка фото к ошибке
```

**Важно про прод:** в `.env` нет ни `TELEGRAM_ERROR_MENU`, ни
`PHOTO_ATTACH_ENABLED`, поэтому боевое поведение — ветка (c): фото уходит
в Lark сразу. Меню ошибки живёт только в конфигурации окружения.

---

## 7. Состояние в памяти

Все хранилища ключуются `(chat_id, user_id)` и имеют TTL.

| Хранилище | TTL | Смысл |
| --- | --- | --- |
| `_pending_status` | 300 с | Ждём описание причины смены статуса |
| `_pending_photo` | 90 с | Фото ждёт текст ошибки |
| `_pending_error_menu` | 900 с | Открыто меню типа ошибки |
| `_pending_error_choice` | 900 с | Ждём номер робота |
| `_pending_custom_text` | 900 с | Режим «описать самому» |
| `_pending_robot_fix` | 180 с | Предложение исправить номер робота |
| `_last_error` | 600 с | Последняя ошибка (фото «после») |
| `_SESSIONS` (дерево) | 1800 с | Сессия дерева решений |

Плюс `_seen_message_ids` (2000) — дедупликация, `_topic_names`/`_routes`
(500) — имена и маршруты топиков.

Чистятся янтарём раз в час (`sweep_pending_status`, `sweep_error_menus`,
`sweep_error_choices`) и секундным потоком удаления.

---

## 8. Данные (Supabase)

| Таблица | Что хранит |
| --- | --- |
| `exceptions_glpc` | Основной журнал: робот, тип, описания, время, смена, склад, `photo_url`. |
| `exceptions` | Связка с роботом/сотрудником (`handle_by`, `robot_id`, времена). |
| `issue_templates` | Шаблоны типов ошибок (`employee_title` — ключ распознавания). |
| `employees` | Сотрудники: `user_name`, `card_id`, `home_warehouse`, `is_leader`. |
| `robots_maintenance_list` | Справочник роботов: статус, тип, текущая проблема. |
| `change_status_robots` | Журнал смен статуса — источник MTTR и простоев. |
| `robots_to_add` | Очередь ненайденных роботов. |
| `warehouses` | Склады (title → id). |
| `telegram_users` | Привязка Telegram → сотрудник. |
| `bot_leases` | Лиз единственного опрашивающего. |

RPC/хранимых процедур нет — только CRUD через PostgREST. Таймаут запросов
10 с, повторов нет. `rest_get_all` листает по 1000 строк, максимум 10 страниц.

**Согласованность важнее полноты:** все функции возвращают `None`/`False`
при сбое, а вызывающий код отличает «база недоступна» от «данных нет»
(например, `send_to_data_base` не ставит робота в очередь при сбое чтения).

---

## 9. Лиз и фоновые задачи

**Лиз** (`bot_leases`): один инстанс опрашивает, второй встаёт в standby
и не запускает планировщики. TTL 90 с, продление каждый цикл + отдельный
поток heartbeat (15 с). Нет таблицы — защита выключена, бот работает.
Сбой базы при захвате — не опрашиваем (нельзя доказать уникальность);
сбой при продлении — продолжаем опрашивать.

**Планировщики** (только у опрашивающего инстанса):

| Задача | Когда | Куда |
| --- | --- | --- |
| Отчёт по смене | 06:00 и 18:00 (окно 15 мин) | Lark, общий вебхук |
| Автозакрытие очереди | каждые 30 мин | только Supabase |
| Дайджест обслуживания | раз в сутки после 09:00 | личка админам |
| Недельный отчёт | понедельник после 08:00 (по умолчанию выключен) | Lark, общий вебхук |

Дедупликация отправок — маркерами в Storage-бакете `bot-reports`.

---

## 10. Production equipment intake

Пошаговый приём ошибок работает в производственном боте:

- фото запускает анкету в разрешённом topic склада;
- Supabase writes use the production service role and the additive schema in `sql/equipment_intake.sql`;
- Lark cards route through the warehouse-specific error webhook even if persistence fails.

**Что именно делает dry-run:** пропускает в Telegram только ответы в топик из
белого списка (проверка по адресу доставки, а не по тексту). Пустой белый
список → не отправляется ничего. Запись в Supabase блокируется отдельно.

---

## 11. Дерево решений

Путь сотрудника:

```
Photo → category → model/type → module (robot/workstation only) → device number → description → confirmation
```

- Роботы: A42T, A42T C2, K50H; модули: Lifting, Rotation, Tray, Chassis.
- Рабочие станции: Pick, Conveyor, Tally; модули: Offline, Wrong task.
- Зарядки: Charge for big robot / Charge for small robot; модулей пока нет.
- Номер оборудования вводится вручную (список техники меняется).
- После модуля — свободный текст: собираем сырые формулировки.
- Итог сохраняется в `telegram_equipment_reports`; неизвестная техника попадает в `telegram_devices_to_add`.

**Расширение:** `/tree` → выбрать узел → добавить вариант. Правки сохраняются в таблице `telegram_intake_options`, общей для всех реплик и сотрудников. Все участники разрешённого чата могут добавлять варианты; удаление и переименование из редактора не выполняются.

---

## 12. Эксплуатация

**Запуск:** `python3 telegram_bot.py`; проверка `curl localhost:7777/health`.

**Деплой:** Railway, Docker (`python:3.11-slim`), healthcheck `/health`,
**1 реплика** (long polling не терпит второй инстанс; от дублей страхует лиз).

**Вебхуки Lark** выбираются по складу и виду: ошибки → `LARK_HOOK_ERROR_*`,
статусы → `LARK_HOOK_STATUS_*`, фолбэк → `LARK_TARGET_HOOK_URL`.
Отчёты по смене и недельный идут в общий вебхук.

**Логи:** `logs/app.log` (ротация 5 МБ × 5), уровень `LOG_LEVEL`.

---

## 13. Известные особенности и риски

Проверено по коду; production не менялся, перечислено для будущих сессий.

**Исправлено 03.10.2026:**

1. ~~`edit_message_text` / `edit_message_caption` не передают
   `message_thread_id`, поэтому в dry-run они всегда блокируются.~~
   **Исправлено:** оба метода принимают `message_thread_id`, все вызовы в боте
   передают топик-источник. Регрессия — `test_edit_message_thread_id_passthrough`.
2. ~~`analytics.send_weekly_report` проверяет `result is None` вместо
   `hook_ok(result)` — вебхук с ненулевым `code` будет считаться успехом
   и отчёт помечается отправленным.~~ **Исправлено:** используется
   `hook_ok(result)`; при ненулевом `code` день не помечается отправленным.
4. ~~`sweep_custom_texts()` не вызывается нигде — записи `_pending_custom_text`
   живут до перезапуска.~~ **Не подтвердилось:** уборка вызывается в
   `images_janitor_loop` раз в час.
5. ~~`_routes` и `_chat_types` растут без ограничения.~~ **Исправлено:**
   запись через `_remember_bounded`, оба словаря ограничены `_CACHE_LIMIT`.
7. ~~`handle_incoming_photo` импортируется, но не вызывается.~~
   **Исправлено:** мёртвый импорт убран из `telegram_bot.py`.

**Остаются в силе:**

3. В dry-run `_rest_post` возвращает `[]` (ложь), хотя комментарий обещает
   «как будто записали» — срабатывает ветка «не удалось сохранить».
6. У `handle_photo` есть второе окно ожидания фото (`PHOTO_HOLD_SECONDS*4`)
   помимо TTL — недокументированная страховка.

**Безопасность:**

15. `SUPABASE_URL` имеет захардкоженное значение по умолчанию (адрес проекта —
    не секрет, оставлено осознанно); ~~ключ по умолчанию пустой, проверки на
    старте нет~~. **Исправлено:** `supabase_key_configured()` и падение
    `main()` с понятным сообщением, если ключ не задан.
17. ~~`BOT_LEASE_ENABLED` не читается в приложении — возможно, устаревшая
    переменная.~~ **Не подтвердилось:** переменной нет ни в `.env.example`,
    ни в коде.