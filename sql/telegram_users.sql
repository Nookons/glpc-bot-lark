-- Привязка Telegram-аккаунтов к сотрудникам (команда /reg в боте).
--
-- Раньше имя сотрудника бот получал из Lark Contact API по user_id,
-- что расходовало квоту Open Platform. Теперь соответствие задаётся
-- один раз в Telegram и хранится здесь.
--
-- Выполнить в Supabase: Dashboard -> SQL Editor -> New query -> Run.

create table if not exists public.telegram_users (
    telegram_id       bigint primary key,
    telegram_username text,
    employee_name     text not null,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);

create index if not exists telegram_users_employee_name_idx
    on public.telegram_users (employee_name);

-- Бот ходит сервисным ключом (service_role), который RLS обходит,
-- поэтому политики не нужны: доступ через anon-ключ будет закрыт.
alter table public.telegram_users enable row level security;

-- Проверка после настройки:
--   select telegram_id, telegram_username, employee_name
--   from public.telegram_users order by employee_name;
