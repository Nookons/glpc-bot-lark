"""Писатель приёма кладёт в `exceptions` только существующие колонки.

**Дефект, найденный 06.10.2026.** Миграция `0068` переименовала колонку
`exception_id` → `template_id` (она ссылается на справочник шаблонов, а не
хранит идентификатор инцидента). Бот продолжал писать старое имя, и **вставка
падала целиком**:

```
column "exception_id" of relation "exceptions" does not exist
```

**Почему это было незаметно.** Запись шла **в две таблицы**: в `exceptions`
(новая схема) и в `exceptions_glpc` (legacy). Вторая проходила, поэтому
инциденты не терялись и ошибок в интерфейсе не было. А `exceptions` стояла на
**7 строках**, хотя бот писал в неё при каждом приёме, — расхождение видно
только сверкой таблиц.

**12.10.2026 переход завершён:** дублирующая запись в `exceptions_glpc` убрана,
`exceptions` — единственный источник. Поэтому проверка изменилась: раньше она
требовала, чтобы второй payload остался нетронутым; теперь второго payload нет
вовсе, и это надо сторожить.
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

    def test_duplicate_legacy_write_is_gone(self) -> None:
        """Дублирующей записи в архив **нет** — иначе две таблицы разойдутся.

        До 12.10.2026 функция писала дважды: новую схему в `exceptions` и старую
        в `exceptions_glpc`. После перехода `exceptions` — единственный источник,
        а `exceptions_glpc` — архив только для чтения. Оставленная вторая запись
        пополняла бы архив, и его расхождение с `exceptions` мы приняли бы за
        дефект данных.

        **Негативная проверка:** вернуть `_rest_post("exceptions_glpc", ...)` —
        тест обязан упасть.
        """
        code = SOURCE.read_text(encoding="utf-8")

        assert "old_obj" not in code, "второй payload вернулся"
        # Ищем именно **аргумент вызова**, а не упоминание в комментарии:
        # пояснение, почему запись убрана, обязано остаться.
        assert '\n        "exceptions_glpc",' not in code, (
            "функция снова пишет в архив отдельной вставкой"
        )
