"""Писатель приёма кладёт в `exceptions` только существующие колонки.

**Дефект, найденный 06.10.2026.** Миграция `0068` переименовала колонку
`exception_id` → `template_id` (она ссылается на справочник шаблонов, а не
хранит идентификатор инцидента). Бот продолжал писать старое имя, и **вставка
падала целиком**:

```
column "exception_id" of relation "exceptions" does not exist
```

**Почему это было незаметно.** Запись идёт **в две таблицы**: в `exceptions`
(новая схема) и в `exceptions_glpc` (legacy). Вторая проходила, поэтому
инциденты не терялись и ошибок в интерфейсе не было. А `exceptions` стояла на
**7 строках**, хотя бот писал в неё при каждом приёме, — расхождение видно
только сверкой таблиц.
"""

from __future__ import annotations

import pathlib
import re

SOURCE = pathlib.Path(__file__).resolve().parents[1] / "sendToDataBase.py"


def _obj_block() -> str:
    """Блок `obj = {...}` — payload для таблицы `exceptions`."""
    code = SOURCE.read_text(encoding="utf-8")
    code = re.sub(r"#[^\n]*", "", code)
    start = code.find("obj = {")

    assert start != -1, "не найден блок obj"

    end = code.find("\n    }", start)

    return code[start : end if end != -1 else start + 2000]


class TestIntakePayloadColumns:
    def test_uses_template_id_not_exception_id(self) -> None:
        """`template_id` — имя после миграции `0068`.

        Ключ проверяется как литерал словаря: `"exception_id":` — именно он
        ломал вставку. Упоминание в комментарии не считается (комментарии
        снимаются в `_obj_block`).
        """
        block = _obj_block()

        assert '"template_id"' in block, "payload потерял ссылку на шаблон"
        assert '"exception_id"' not in block, (
            "бот пишет `exception_id`, которого в таблице нет — вставка упадёт"
        )

    def test_payload_has_the_core_fields(self) -> None:
        """Без этих полей строка не описывает инцидент."""
        block = _obj_block()

        for field in ("robot_id", "handle_by", "start_time", "warehouse"):
            assert f'"{field}"' in block, f"payload потерял {field}"

    def test_legacy_payload_is_untouched(self) -> None:
        """Правка не должна была тронуть второй payload.

        `old_obj` пишется в `exceptions_glpc`, где колонки **старые** —
        переименование `0068` касается только `exceptions`.
        """
        code = SOURCE.read_text(encoding="utf-8")

        assert '"error_robot"' in code
        assert '"error_start_time"' in code
