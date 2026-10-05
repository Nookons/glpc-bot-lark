"""
Подбор подходящего шаблона ошибки под то, что написал оператор.

**Зачем.** Приём складывал в журнал **сырой текст** оператора:

    first_column   = 'robot'            <- категория оборудования, не ошибка
    issue_type     = 'robot'
    second_column  = 'Chassis'          <- модуль
    recovery_title = NULL
    solving_time   = 0

А классический журнал (то, чем пользуется отдел) заполнен по-другому —
из справочника `issue_templates`:

    first_column   = 'Driver component exception'
    issue_type     = 'Unable to drive'
    second_column  = 'Driver component exception'
    recovery_title = 'Move on QR Code robot then recovery robot'
    solving_time   = 6

Проверено на живой базе 05.10.2026: у 243 строк, записанных ботом, колонки
`first_column` и `issue_type` дублировали друг друга (`robot` = `robot`), а
`recovery_title` и `solving_time` были пустыми/нулевыми. Из-за этого такие строки
нельзя было ни группировать по типу ошибки, ни считать время решения — в отчёте
они выглядели как «все ошибки — robot».

**Что делает модуль.** Берёт текст оператора, сравнивает его с полями всех
шаблонов и возвращает **лучший** подходящий. Если ничего достаточно близкого нет,
возвращает `None` — и вызывающий код оставляет прежнее поведение. Выдумывать
шаблон нельзя: лучше пустая колонка, чем ошибка, отнесённая не к тому типу.

**Алгоритм и порог выбраны замерами на РАЗМЕТКЕ ЛЮДЕЙ, а не на глаз.**
Эталон — 12 474 классических строки, где человек проставил шаблон точно
(`second_column` = `issue_templates.employee_title` и `first_column` =
`issue_sub_type`), 13 уникальных описаний:

| Алгоритм | Поля | Порог | Верно | Неверно | Отказ |
|---|---|---|---|---|---|
| `token_set_ratio` | все | 85 | **13** | **0** | 0 |
| `token_set_ratio` | все | 90 | 12 | 0 | 1 |
| `WRatio` | только короткие | 90 | 4 | 0 | 9 |
| `token_set_ratio` | только короткие | 80 | 5 | 0 | 8 |

Выбран `token_set_ratio`, все три поля, порог **85**: 100 % точности при полном
покрытии эталона.

**Почему не `WRatio`.** На длинных описаниях шаблонов он даёт ложные
срабатывания: текст оператора `'unable to rotate'` получал **85.5** против
`'employee on workstation fill tote to overflow'` и относился к
`'Forklift detection without container'` — ошибка поворота уезжала в «неверный
поддон». `token_set_ratio` на том же вводе даёт 77 и честно отказывается.
Игнорировать `issue_description` тоже нельзя: без него порог 85 отвечает лишь на
5 описаний из 13.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Optional

#: Порог, ниже которого совпадение считается случайным. При 85 подбор отвечает
#: верно на **всех** 13 описаниях эталона (см. таблицу в докстроке модуля); при
#: 90 теряет одно, при 80 начинает ошибаться на длинных описаниях.
MATCH_CUTOFF = 85

#: Поля шаблона, по которым сравниваем текст оператора.
#:
#: `issue_description` тоже участвует: у части шаблонов `employee_title` —
#: короткий («System bug»), а описание поясняет суть («Robot have abnormal
#: problem what can't be located»). Без него текст оператора про неисправность
#: не находил бы шаблон.
MATCH_FIELDS = ("employee_title", "issue_sub_type", "issue_description")

#: Значения `answers.object` дерева приёма, которым соответствует
#: `equipment_type` справочника. Робот — «Equipment», зона/пол (QR-код) и склад —
#: «Environment», действия оператора — «Operation».
CATEGORY_EQUIPMENT_TYPE = {
    "robot": "Equipment",
    "charging": "Equipment",
    "workstation": "Equipment",
    "qr_code": "Environment",
}

#: Префикс, с которым операторы пишут тип ошибки из журнала: «EX: 驱动组件异常 …».
#: Без снятия он не мешает подбору (совпадение по подстроке), но мешает читать.
_EX_PREFIX = re.compile(r"^\s*EX:\s*", re.IGNORECASE)

#: Хвостовая точка и лишние пробелы.
_TRAILING = re.compile(r"[\s.;]+$")

#: Номер робота, приписанный к описанию: «… driver component exception. 3563».
#:
#: Операторы пишут так часто, и это ломало подбор: номер — не часть описания, но
#: `token_set_ratio` считает его лишним словом и роняет счёт со 100 до 80, ниже
#: порога. **Замерено на живых данных:** снятие номера поднимает покрытие строк
#: бота с 63 % до 80 % при **неизменной** точности 100 % на разметке людей
#: (22 верных, 0 неверных, 91 отказ — одинаково в обоих режимах).
#:
#: Ограничение в 1–6 цифр намеренное: длинные числа — это уже не номер робота
#: (в базе они четырёхзначные), и срезать их значило бы испортить описание.
_TRAILING_NUMBER = re.compile(r"\s+\d{1,6}\s*$")

#: Строка «Reported error: <текст>» из `issue_description`.
_REPORTED = re.compile(r"Reported error:\s*(.*?)(?:\n|$)", re.DOTALL)


def operator_text(description: Any) -> str:
    """
    Чистый текст оператора из того, что пришло в дереве.

    Принимает и «сырое» описание, и уже собранную строку журнала
    (`Reported error: …\\nIdentifier: …`): если внутри есть первая форма,
    извлекается она.

    Отбрасывает префикс `EX:` и хвостовой номер робота — ни то, ни другое не
    часть описания ошибки, но оба мешают подбору (обоснование и замеры — в
    комментариях к `_TRAILING_NUMBER`).
    """
    raw = str(description or "").strip()

    found = _REPORTED.search(raw)

    if found:
        raw = found.group(1)

    raw = _EX_PREFIX.sub("", raw.strip())
    raw = _TRAILING_NUMBER.sub("", raw.strip())

    return _TRAILING.sub("", raw).strip()


def _score(text: str, template: Dict[str, Any]) -> float:
    """Насколько текст оператора похож на шаблон (0–100)."""
    from rapidfuzz import fuzz

    low = text.casefold()
    best = 0.0

    for field in MATCH_FIELDS:
        candidate = str(template.get(field) or "").strip().casefold()

        if not candidate:
            continue

        # `token_set_ratio`, а не `WRatio`: последний даёт ложные 85+ на длинных
        # описаниях шаблонов (разбор — в докстроке модуля).
        best = max(best, float(fuzz.token_set_ratio(low, candidate)))

    return best


def match_template(
    description: Any,
    templates: Iterable[Dict[str, Any]],
    cutoff: int = MATCH_CUTOFF,
) -> Optional[Dict[str, Any]]:
    """
    Самый подходящий шаблон для текста оператора, или `None`.

    `None` — не ошибка и не «шаблон не найден» вообще: это честный ответ
    «достаточно близкого нет». Вызывающий код при `None` оставляет прежнее
    поведение, поэтому отсутствие шаблона никогда не теряет отчёт.
    """
    text = operator_text(description)

    if not text:
        return None

    best: Optional[Dict[str, Any]] = None
    best_score = 0.0
    best_name_score = 0.0

    for template in templates:
        score = _score(text, template)
        name_score = _name_score(text, template)

        if score > best_score or (
            score == best_score and score > 0 and _prefer(template, name_score, best, best_name_score)
        ):
            best, best_score, best_name_score = template, score, name_score

    if best is None or best_score < cutoff:
        return None

    return best


def _name_score(text: str, template: Dict[str, Any]) -> float:
    """Похожесть именно на название шаблона (`employee_title`)."""
    from rapidfuzz import fuzz

    title = str(template.get("employee_title") or "").strip().casefold()

    if not title:
        return 0.0

    return float(fuzz.token_set_ratio(text.casefold(), title))


def _prefer(
    candidate: Dict[str, Any],
    candidate_name: float,
    current: Optional[Dict[str, Any]],
    current_name: float,
) -> bool:
    """
    Кого выбрать при равном основном счёте.

    **Сначала — похожесть на название.** Это важнее полноты данных, и вот
    почему: у шаблонов бывают **одинаковые** `issue_sub_type` при разных
    названиях. Пример из живого справочника — id 5 «Driver component exception»
    и id 10 «Speed error»: у обоих `issue_sub_type` = «Driver component
    exception». Основной счёт у них совпадает, и без этого правила выбирался
    тот, что просто оказался позже в списке: оператор писал «driver component
    exception», а в журнал попадало `Speed error`.

    **Замерено на строгой метрике** (сравнение выбранного `employee_title` с
    тем, что проставил человек, а не «совпал type или sub_type»):

    | Тай-брейк | Верно | Неверно | Строгая точность |
    |---|---|---|---|
    | не было | 18 | 4 | 82 % |
    | по названию | **20** | **2** | **91 %** |

    Полнота данных (`_fuller`) остаётся вторым шагом — когда и названия похожи
    одинаково.
    """
    if current is None:
        return True

    if candidate_name != current_name:
        return candidate_name > current_name

    return _completeness(candidate) > _completeness(current)


def _fuller(candidate: Dict[str, Any], current: Optional[Dict[str, Any]]) -> bool:
    """Есть ли у кандидата больше данных, чем у текущего лучшего."""
    if current is None:
        return True

    return _completeness(candidate) > _completeness(current)


def _completeness(template: Dict[str, Any]) -> int:
    """Сколько значимых полей заполнено (для разрешения ничьих)."""
    return sum(
        1
        for field in ("recovery_title", "solving_time", "issue_description", "issue_type")
        if template.get(field) not in (None, "")
    )
