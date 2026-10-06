"""Карточка робота читает таблицу, в которую **действительно пишут**.

**Дефект, найденный 06.10.2026.** В `robot_card.py` стояло
`EXCEPTIONS_TABLE = "exceptions"` — таблица под переход (`0068`/`0069`).
Переход **не сделан**, писателя в ней нет: 7 строк против 24 185 в
`exceptions_glpc`. Бот приёма и API пишут в legacy.

Хуже: у `exceptions` **нет колонок** `start_time`, `exception_id`, `robot_id`,
которые запрашивал код. Запрос падал, и карточка робота показывала ноль ошибок
при 15 реальных. Проверено на живой базе.
"""

from __future__ import annotations

import pathlib
import re

SOURCE = pathlib.Path(__file__).resolve().parents[1] / "robot_card.py"


def _code() -> str:
    """Исходник без комментариев: иначе проверка засчитает текст пояснения."""
    text = SOURCE.read_text(encoding="utf-8")
    text = re.sub(r'""".*?"""', "", text, flags=re.S)
    return re.sub(r"#[^\n]*", "", text)


class TestRobotCardTable:
    def test_reads_the_legacy_table(self) -> None:
        """Источник — `exceptions_glpc`: только туда идёт запись."""
        assert 'EXCEPTIONS_TABLE = "exceptions_glpc"' in _code()

    def test_does_not_read_the_empty_table(self) -> None:
        assert 'EXCEPTIONS_TABLE = "exceptions"' not in _code()

    def test_uses_legacy_column_names(self) -> None:
        """Колонки legacy, а не новой таблицы.

        В `exceptions_glpc` начало ошибки — `error_start_time`, робот —
        `error_robot`. Прежний код запрашивал `start_time` и `robot_id`, которых
        в таблице нет, — запрос падал.
        """
        code = _code()

        assert "error_start_time" in code
        assert "error_robot" in code
        for missing in ('"start_time"', '"robot_id"', '"exception_id"', '"handle_by"'):
            assert missing not in code, f"осталась колонка чужой таблицы: {missing}"

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

        assert '"error_robot"' in block, "фильтр потерял робота"
        assert "warehouse" in block, "фильтр ошибок потерял склад — будут чужие ошибки"
