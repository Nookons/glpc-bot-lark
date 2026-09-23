-- Индексы для запросов бота к журналу исключений.
--
-- 1) Отчёты и счётчик смены фильтруют по (issue_data, shift_type, warehouse),
--    а счётчик ещё и по конкретному роботу. Без индекса это полный скан на
--    каждое сообщение.
-- 2) Уникальный индекс по uniq_key делает вставку идемпотентной: при
--    повторной доставке апдейта (SIGKILL, сбой сохранения offset) дубль
--    не появится — код вставляет с resolution=ignore-duplicates.
--
-- Выполнить в Supabase: Dashboard -> SQL Editor -> New query -> Run.

create index if not exists exceptions_glpc_shift_idx
    on public.exceptions_glpc (issue_data, shift_type, warehouse, error_robot);

create unique index if not exists exceptions_glpc_uniq_key_uidx
    on public.exceptions_glpc (uniq_key);

-- Проверка:
--   select indexname from pg_indexes
--   where tablename = 'exceptions_glpc';
