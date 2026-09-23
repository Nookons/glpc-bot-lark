-- Фото, привязанное к записи об ошибке.
--
-- Сотрудник присылает фото робота и текст ошибки (до или после фото) —
-- бот складывает это в одну запись: фото уходит в Supabase Storage,
-- а в записи хранится ссылка.
--
-- Выполнить в Supabase: Dashboard -> SQL Editor -> New query -> Run.

alter table public.exceptions_glpc
    add column if not exists photo_url text;

alter table public.exceptions
    add column if not exists photo_url text;

-- Проверка:
--   select id, error_robot, employee, photo_url
--   from public.exceptions_glpc
--   where photo_url is not null
--   order by created_at desc
--   limit 5;
