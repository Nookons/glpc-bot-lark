-- Лиз «единственного опрашивающего Telegram».
--
-- Двум запущенным ботам (например, локально и на сервере) Telegram может
-- отдать один и тот же апдейт: он считается подтверждённым только при
-- следующем getUpdates с большим offset. Из-за этого сообщения
-- обрабатываются дважды — дубли в exceptions_glpc и в Lark-группе.
--
-- Перед стартом polling бот занимает строку в этой таблице и продлевает её.
-- Второй инстанс видит живой лиз и встаёт в режим standby.
--
-- Выполнить в Supabase: Dashboard -> SQL Editor -> New query -> Run.

create table if not exists public.bot_leases (
    name         text primary key,
    holder       text not null,
    heartbeat_at timestamptz not null default now()
);

-- Бот ходит сервисным ключом (service_role), он RLS обходит.
alter table public.bot_leases enable row level security;

-- Проверка:
--   select name, holder, heartbeat_at from public.bot_leases;
