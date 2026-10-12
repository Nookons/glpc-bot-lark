"""Карточка робота читает **единый** журнал `exceptions`.

**История.** 06.10.2026 карточка читала `exceptions`, но тогда переход не был
сделан: писателя в таблице не было (7 строк против 24 185 в `exceptions_glpc`),
шёл поиск по колонкам `start_time`/`robot_id`, которых в таблице не было, и
карточка показывала ноль ошибок при 15 реальных. Источник вернули на legacy.

**12.10.2026 переход завершён** (перенос `0069` + перевод писателей), поэтому
карточка снова читает `exceptions` — и это верно. Имя таблицы берётся из общего
`journal`, чтобы читатели не разошлись.
"""

from __future__ import annotations

import pathlib
import re

SOURCE = pathlib.Path(__file__).resolve().parents[1] / "robot_card.py"
JOURNAL = pathlib.Path(__file__).resolve().parents[1] / "journal.py"


def _code() -> str:
    """Исходник без комментариев: иначе проверка засчитает текст пояснения."""
    text = SOURCE.read_text(encoding="utf-8")
    text = re.sub(r'""".*?"""', "", text, flags=re.S)
    return re.sub(r"#[^\n]*", "", text)


class TestRobotCardTable:
    def test_reads_the_unified_table(self) -> None:
        """Источник — `journal.EXCEPTIONS_TABLE` (единый журнал).

        Проверяется связь с общим модулем, а не конкретная строка: если имя
        таблицы снова поменяют, менять придётся **одно** место — `journal`, а не
        каждого читателя по отдельности (именно так карточка и осталась бы на
        архиве незамеченной).
        """
        code = _code()

        assert "EXCEPTIONS_TABLE = journal.EXCEPTIONS_TABLE" in code
        # Общий модуль действительно указывает на `exceptions`.
        assert 'EXCEPTIONS_TABLE = "exceptions"' in JOURNAL.read_text(encoding="utf-8")

    def test_does_not_read_the_archive_table(self) -> None:
        assert "exceptions_glpc" not in _code(), "карточка вернулась на архив"

    def test_uses_the_new_column_names(self) -> None:
        """Колонки единой таблицы: `start_time`, `robot_number`, `warehouse` id.

        Прежний код запрашивал `error_start_time`/`error_robot` — имён, которых
        в `exceptions` нет: запрос падал бы, и карточка показывала ноль ошибок.
        """
        code = _code()

        assert "start_time" in code
        assert "robot_number" in code
        for missing in ('"error_start_time"', '"error_robot"', '"exception_id"'):
            assert missing not in code, f"осталось имя чужой таблицы: {missing}"

    def test_scopes_by_warehouse(self) -> None:
        """Склад обязателен **в фильтре ошибок**, а не где-то ещё.

        Номера роботов повторяются между складами (`121`, `122`, `123` есть и в
        GLP-C, и в SMALL-P3; `201`, `2000` — в GLP-C и P3-DC-1). Без склада
        карточка показала бы чужие ошибки.

        **Проверяется именно блок `errors_filter`.** Прежняя версия теста искала
        строку по всему файлу — и проходила, когда склад убирали из фильтра, но
        оставляли в запросе робота (проверено негативно: 0 упавших).
        """
        code = _code()
        start = code.find("errors_filter")

        assert start != -1, "в коде нет блока errors_filter"

        # Беру окно до следующей закрывающей скобки **на нулевом отступе**.
        # `\{.*?\}` здесь не годится: первая же `}` встречается внутри
        # `f"eq.{robot.get('id')}"`, и поиск обрывался на ней — тест проходил,
        # даже когда склад из фильтра убрали (проверено негативно: 0 упавших).
        window = code[start : start + 400]
        end = window.find("\n    }")
        block = window if end == -1 else window[: end + 6]

        assert '"robot_number"' in block, "фильтр потерял робота"
        assert "warehouse" in block, "фильтр ошибок потерял склад — будут чужие ошибки"
        assert 'journal.warehouse_id' in code, (
            "склад обязан переводиться в числовой id: название в bigint-колонку "
            "не сравняется, и карточка покажет ноль ошибок"
        )
