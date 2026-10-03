# Equipment error intake (production)

The production Telegram bot starts this flow for every photo in a monitored warehouse topic. It collects equipment category and type, module where configured, device number, and free text; confirmation writes a report to Supabase and independently attempts a warehouse-routed Lark card.

Employees can use `/tree` to add choices to an existing choice node. Added choices are stored in `telegram_intake_options` in Supabase and become available without restarting the bot. The editor only adds choices; it does not rename or delete existing ones.

Before deploying, apply `sql/equipment_intake.sql` to the production Supabase database using the Supabase SQL editor. The migration is additive and creates the report, unknown-device review queue, and shared option tables. The bot uses its configured service-role key for PostgREST access.

Robot module choices: Lifting, Rotation, Tray, Chassis. Workstation module choices: Offline, Wrong task. Chargers have no module step.
