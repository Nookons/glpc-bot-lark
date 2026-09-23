# Telegram-бот приёма ошибок роботов → Lark-группа

Бот переехал с Lark-событий на Telegram. Причина: каждое сообщение в Lark
тянуло за собой вызовы Open Platform API (имя сотрудника, ответ в чат,
скачивание фото), а квота тенанта — 10 000 вызовов в месяц — исчерпана.

Главное: **вывод в Lark-группу идёт через custom-bot webhook**
(`bot/v2/hook/...`), который квоту Open Platform не расходует.

## Как это работает теперь

```
сотрудник → Telegram-группа (бот-админ)
              │  long polling getUpdates
              ▼
        telegram_bot.py
              ├── /reg          → привязка Telegram → employees (Supabase)
              ├── разбор текста → error_parser.parse_error_message
              ├── запись        → Supabase: exceptions + exceptions_glpc
              ├── смена/счётчик  → shift.py + Supabase
              └── вывод         → Lark-группа через webhook (0 вызовов API)
```

| Действие | Было (Lark) | Стало (Telegram) |
|---|---|---|
| Приём сообщений | события `im.message.receive_v1` | long polling Telegram |
| Имя сотрудника | Contact API (вызов) | таблица `telegram_users` (0 вызовов) |
| Ответ в чат («не распознал», алерт) | `im/v1/messages` (вызов) | `sendMessage` в Telegram (0 вызовов) |
| Карточка ошибки в группу | webhook | webhook |
| Скачивание фото | `im/v1/resources` (вызов) | Telegram `getFile` (0 вызовов) |
| Отправка фото в группу | `im/v1/images` (вызов) | `im/v1/images`, а при ошибке квоты — ссылка через Supabase Storage (0 вызовов) |

Фильтр по топику: сообщения принимаются только из топика `Ex GLPC`
(`TELEGRAM_TOPIC_ID`), команды — из любого топика; ответы уходят в топик-источник.

Квоту Lark тратит только загрузка фото, и лишь когда она доступна: при
исчерпанной квоте фото автоматически уходит ссылкой, и тогда расход нулевой.

## Что нужно сделать

### 1. Таблица привязки в Supabase

Supabase → SQL Editor → выполнить `sql/telegram_users.sql`.

### 2. Переменные окружения

Скопировать `.env.example` в `.env` и заполнить:

```
TELEGRAM_BOT_TOKEN=...        # токен от @BotFather
SUPABASE_URL=...
SUPABASE_SERVICE_KEY=...
LARK_APP_ID=...               # только для фото
LARK_APP_SECRET=...           # только для фото
LARK_TARGET_HOOK_URL=...      # webhook целевой группы (по умолчанию уже зашит)
TELEGRAM_TOPIC_NAME=Ex GLPC   # опционально: имя топика
# TELEGRAM_TOPIC_ID=27        # id топика из /id (надёжный фильтр)
```

### 3. Бот в Telegram

1. Токен от [@BotFather](https://t.me/botfather) уже есть — вставить в `.env`.
2. Добавить бота **в группу** и выдать права администратора.
   Privacy mode выключать не нужно: админ получает все сообщения группы
   ([docs](https://core.telegram.org/bots/features#privacy-mode)).
3. Список команд бот публикует сам при старте (`setMyCommands`).

### 4. Топик Ex GLPC (форум-группа)

Бот обрабатывает сообщения **только из одного топика**. Команды `/id`, `/reg`,
`/help` работают в любом топике — чтобы можно было настроиться, а ошибки и
фото принимаются только из нужного.

Как включить фильтр:

1. Выдать боту **права администратора** в группе (иначе он не видит
   сообщения топиков: privacy mode у ботов включён по умолчанию, но у
   администраторов он не действует).
2. Отправить `/id` **внутри топика `Ex GLPC`** — бот ответит
   `message_thread_id` (например `27`).
3. Вписать его в `.env`:

   ```
   TELEGRAM_TOPIC_ID=27
   ```

4. Перезапустить бота. `/id` в этом топике теперь покажет
   `✅ this topic is monitored (id-match)`.

`/id` и `/help` отвечают даже в чате вне `TELEGRAM_ALLOWED_CHAT_IDS` — чтобы
id всегда можно было узнать, ничего не отключая.

Почему по id, а не по имени: в Bot API **нет** метода, который отдаёт имя
топика по `message_thread_id` (есть только `createForumTopic`,
`editForumTopic`, `getForumTopicIconStickers`). Поэтому `TELEGRAM_TOPIC_NAME`
работает как запасной вариант и только если бот уже видел сервисное сообщение
о создании/переименовании топика (имя запоминается в память процесса).

Ответы бота автоматически уходят **в тот же топик**, откуда пришло сообщение —
включая алерты «отправить в обслуживание» и подтверждения записи.

### 5. Запуск

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 telegram_bot.py
```

Проверка: `curl localhost:7777/health` → `{"status":"ok","bot":"..."}`.

### 6. Деплой на Railway

Сервис собран из GitHub и деплоится автоматически по пушу в `main`:

| | |
|---|---|
| Проект / окружение | `adaptable-hope` → `production` |
| Сервис | `glpc-bot-lark` |
| Репозиторий | `Nookons/glpc-bot-lark`, ветка `main` |
| URL | https://glpc-bot-lark-production.up.railway.app |
| Healthcheck | `/health` (задан в `railway.json`) |
| Реплики | 1 (long polling не терпит второй инстанс) |

`railway.json` задаёт команду старта, healthcheck и рестарт по ошибке;
`Procfile` и `Dockerfile` указывают на тот же `telegram_bot.py`.

Переменные окружения живут в Railway → Variables (`.env` в репозиторий не
попадает): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_TOPIC_ID`, `TELEGRAM_TOPIC_NAME`,
`TELEGRAM_ALLOWED_CHAT_IDS`, `LARK_APP_ID`, `LARK_APP_SECRET`, `SUPABASE_URL`,
`SUPABASE_SERVICE_KEY`, `SUPABASE_PHOTO_BUCKET`, `TELEGRAM_CONFIRM_TTL`.

Обновить переменные из локального `.env` через CLI:

```bash
SVC=glpc-bot-lark
TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' .env | cut -d= -f2-)
printf '%s' "$TOKEN" | railway variable set TELEGRAM_BOT_TOKEN --stdin --service "$SVC"
railway variable set TELEGRAM_TOPIC_ID=2 --service "$SVC"
```

> ⚠️ Telegram long polling допускает **только один** запущенный инстанс:
> держите 1 реплику и не запускайте бота локально одновременно с Railway,
> иначе в логах будет `Conflict: terminated by other getUpdates request`.

## Команды бота

| Команда | Что делает |
|---|---|
| `/reg Иван Петренко` | привязать Telegram к имени из `employees.user_name` |
| `/whoami` | показать текущую привязку |
| `/unreg` | удалить привязку |
| `/stats [YYYY-MM-DD] [day\|night]` | статистика смены (по умолчанию текущая) |
| `/offline <robot>` | отправить робота в офлайн (причина кнопками + описание) |
| `/online <robot>` | вернуть робота в работу (причина + описание) |
| `/cancel` | отменить незавершённую смену статуса |
| `/id` | показать `chat_id`, `user_id`, `message_thread_id`, имя топика и отслеживается ли он |
| `/help` | справка |

При `/reg` имя ищется сначала точно, затем нечётко (rapidfuzz, порог 78) —
при опечатке бот предложит похожие имена из таблицы `employees`.

## Защита от дублей (один Telegram — один бот)

Telegram long polling допускает только один активный `getUpdates`: апдейт
считается подтверждённым лишь при следующем запросе с бо́льшим offset. Поэтому
два запущенных инстанса (например, локально и на Railway) успевают обработать
одно и то же сообщение дважды — дубли в `exceptions_glpc` и в Lark-группе.

Перед стартом бот занимает лиз в таблице `bot_leases`
(миграция: `sql/bot_leases.sql`):

* свободен или просрочен → инстанс становится опрашивающим;
* занят живым инстансом → процесс уходит в **standby**: `getUpdates` не
  вызывается, сообщения и отчёт за смену он не трогает (отчёт шлёт только
  опрашивающий, иначе были бы двойные отчёты);
* лиз продлевается на каждой итерации polling (`BOT_LEASE_TTL`, по умолчанию
  120 секунд); если продлить не удалось — polling останавливается сам;
* `/health` показывает роль процесса: `"poller": "poller"` или
  `"poller": "standby (<holder>)"`.

Если таблицы нет, бот продолжает работать, но пишет предупреждение в лог: пока
миграция не применена, защиты от дублей нет.

## Служебные сообщения бота

Подтверждения (`✅ Saved: robot …`, `✅ Photo forwarded to Lark`, а также
статусы «не смог скачать/переслать фото») бот **удаляет за собой** через
`TELEGRAM_CONFIRM_TTL` секунд — по умолчанию 10, `0` отключает удаление.
Так группа не зарастает «хвостами»: сотрудник видит мгновенную реакцию,
и через десять секунд остаётся только его собственное сообщение.

Важное и обучающее не удаляется: подсказка про `/reg`, подсказка по формату
сообщения, `/help`, `/id`, `/stats`, ответы на команды и алерт
«отправить в обслуживание» (его читает вся смена).

## Смена статуса робота (офлайн/онлайн)

```
/offline 3680
   → бот показывает робота, текущий статус и кнопки причин
   → тап по причине (например 行走异常/Abnormal walking)
   → бот просит описать причину сообщением
   → сотрудник пишет причину
   → бот удаляет свои сообщения, меняет статус в базе
     и отправляет карточку в Lark-группу
```

Что пишется в базу (туда же, куда пишет приложение склада):

| Таблица | Что |
|---|---|
| `robots_maintenance_list` | `status` = `在线 \| Online` / `离线 \| Offline`, `updated_at`, `updated_by` |
| `change_status_robots` | журнал: `old_status` → `new_status`, `type_problem`, `problem_note`, `robot_id`, `add_by`, `warehouse` |

Причины берутся из вашего же журнала (кнопками, чтобы данные не разъезжались):
в офлайн — `行走异常/Abnormal walking`, `小车车身部件撞坏/Damaged car body parts`,
`Safety controller issues`, `其他 / Other`; в онлайн — `Solved without changing`,
`更换备件 / Replaced Spare Parts`, `软件升级 / Software Upgrade`, `Software fix`, `其他 / Other`.

Детали: может любой сотрудник с привязкой `/reg` (автор пишется его `card_id` из
`employees`); если робот уже в этом статусе — бот откажет и покажет текущий;
меняются только роботы склада `GLP-C`; незавершённый флоу живёт
`TELEGRAM_STATUS_FLOW_TTL` секунд (по умолчанию 300) и сбрасывается командой
`/cancel` или любой другой командой.

## Формат сообщения об ошибке

```
<issue type>: <description>. <robot number>
```

Пример:

```
Unable to drive: Security module failure. 3780
```

Порядок обработки: проверка привязки → разбор формата → проверка, что номер
робота состоит из цифр → запись в Supabase (с фаззи-поиском шаблона) →
пересылка карточки в Lark → при `count >= ERROR_THRESHOLD` алерт
«отправить в обслуживание».

Если робота нет в `robots_maintenance_list` (склад `GLP-C`), запись в базу
невозможна — номер ставится в очередь `robots_to_add`. Но ошибка **всё равно
уходит в Lark-группу** с пометкой `Robot … (not in the system)` и пояснением,
что робот не найден и в базу запись не попала: смена видит проблему, а не
теряет её. Сотрудник получает в Telegram сообщение, что робот не найден и
ошибка переслана.

## Фотографии

Фото из Telegram скачиваются в `images/` и уходят в Lark-группу двумя путями:

1. **Картинкой** (штатный): загрузка в Lark `im/v1/images` → вебхук шлёт
   `post` с картинкой и подписью «📷 Photo from <имя>». Это **единственный**
   вызов, расходующий квоту Open Platform — 1 на фото.
2. **Ссылкой** (автоматический резерв): если Lark API ответил ошибкой
   (например, `code 99991403 This month's API call quota has been exceeded`),
   фото грузится в Supabase Storage (`bot-photos`) и в группу уходит текст с
   подписью и ссылкой. Квота Lark не расходуется вообще.

Бот сообщает в Telegram, каким путём ушло фото:
`✅ Photo forwarded to Lark` или `✅ Photo sent to Lark as a link`.

Bucket создаётся автоматически при первой загрузке; настройки —
`SUPABASE_PHOTO_BUCKET`, `SUPABASE_PHOTO_BUCKET_PUBLIC`,
`SUPABASE_SIGNED_URL_TTL` (приватный bucket + signed-ссылка по умолчанию,
чтобы фото не были публично доступны).

## Диагностика

| Симптом | Причина / решение |
|---|---|
| `TELEGRAM_BOT_TOKEN is not set` | не заполнен `.env` |
| `Can't reach Telegram with the configured token` | неверный токен |
| `⚠️ Can't save the link right now` | не выполнена `sql/telegram_users.sql` |
| `/health` → `"users_table": false` | таблица `telegram_users` не создана (бот скажет об этом и в консоли при старте) |
| `code 99991403` в логах | квота Lark исчерпана — фото уйдут ссылкой, остальное работает |
| `⚠️ Employee ... not found` | имени нет в `employees.user_name` — проверить написание |
| Бот молчит в группе | бот не админ группы, либо сообщение не из топика `Ex GLPC` |
| `I can't identify this topic yet` | не задан `TELEGRAM_TOPIC_ID` — сделать `/id` в топике и вписать id |
| `Conflict: terminated by other getUpdates` | запущено два инстанса (Railway + локально) |
| Фото не уходит в Lark | не заданы `LARK_APP_ID`/`LARK_APP_SECRET` или кончилась квота |

Логи: stdout (Railway) и `logs/app.log` (ротация 5 МБ × 5).

## Тесты

Offline-проверка логики без сети, токена и Supabase:

```bash
python3 tests/test_telegram_offline.py
```

Покрывает: разбор команд (`/reg@Bot`), регистрацию и подсказки имён,
основной поток сохранения ошибки, алерт по порогу, фото, защиту от дублей и
старых сообщений, белый список чатов, notifier вместо Lark API.

## Файлы

| Файл | Назначение |
|---|---|
| `telegram_bot.py` | entrypoint: polling, команды, поток ошибки, `/health`, `/shift_stats` |
| `telegram_api.py` | клиент Telegram Bot API (long polling, файлы) |
| `telegram_store.py` | привязка Telegram → сотрудник в Supabase |
| `error_parser.py` | разбор формата сообщения |
| `sendToDataBase.py` | запись исключений в Supabase + `notify_user` |
| `pending_photos.py` | пересылка в Lark через webhook |
| `lark_media.py` | webhook-хелперы + загрузка изображений |
| `shift.py`, `shift_report.py` | смены и отчёт в конце смены |
| `sql/telegram_users.sql` | миграция таблицы привязок |

Старая Lark-версия входа (`webhookApp.py`, `getUserName.py`,
`donwloadImage.py`) удалена — при необходимости её можно достать из истории
git.
