# -*- coding: utf-8 -*-
"""Тесты слоя хранения редактора дерева (`equipment_intake/storage.py`).

Зачем отдельный файл. Полный режим редактора (`update_option`,
`set_option_hidden`, `delete_option`, `move_option`, `update_node`) появился в
коммите `dbba8bc` — это ~2200 строк, — но живого покрытия у него не было:
`sql/intake_editor_v2.sql` к базе не применялась, поэтому в проде он выключен,
а тесты задевали только ветку «база не готова». Здесь функции проверяются
напрямую с базой в памяти.

База подменяется тремя функциями `sendToDataBase`, которые storage импортирует
лениво внутри вызовов: `rest_get`, `rest_upsert`, `rest_delete`.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import equipment_intake.storage as storage  # noqa: E402
from equipment_intake.tree_config import (
    DEFAULT_TREE,
    MODULE_ERRORS,
    cause_node_id,
    error_node_id,
)  # noqa: E402


class FakeDB:
    """Таблицы в памяти: строки options и nodes по ключу."""

    def __init__(self) -> None:
        self.rows: dict[str, list[dict]] = {}

    # --- sendToDataBase API, которым пользуется storage ---
    def rest_get(self, table, params=None, optional=False):
        params = params or {}
        out = list(self.rows.get(table, []))
        for key, value in params.items():
            if key in ("select", "order", "limit", "offset"):
                continue
            if isinstance(value, str) and value.startswith("eq."):
                want = value[3:]
                out = [r for r in out if str(r.get(key)) == want]
        return out

    def rest_upsert(self, table, payload, on_conflict):
        rows = self.rows.setdefault(table, [])
        keys = [k.strip() for k in on_conflict.split(",")]
        for row in rows:
            if all(str(row.get(k)) == str(payload.get(k)) for k in keys):
                row.update(payload)
                break
        else:
            rows.append(dict(payload))
        return [payload]

    def rest_delete(self, table, params=None):
        params = params or {}
        rows = self.rows.setdefault(table, [])
        keep = []
        removed = 0
        for row in rows:
            match = True
            for key, value in params.items():
                if isinstance(value, str) and value.startswith("eq."):
                    if str(row.get(key)) != value[3:]:
                        match = False
                elif row.get(key) != value:
                    match = False
            if match:
                removed += 1
            else:
                keep.append(row)
        self.rows[table] = keep
        return bool(removed)

    def rest_patch(self, table, params=None, payload=None):
        """Правка строки — как `PATCH` у PostgREST.

        Нужна заготовкам `issue_templates`: редактор правит их **по одному
        полю**, и заглушка обязана это поведение поддерживать, иначе тесты
        «проходят» на пустой базе и ничего не проверяют.
        """
        params = params or {}
        rows = self.rows.setdefault(table, [])
        updated = []
        for row in rows:
            match = all(
                str(row.get(key)) == value[3:]
                for key, value in params.items()
                if isinstance(value, str) and value.startswith("eq.")
            )
            if match:
                row.update(payload or {})
                updated.append(dict(row))
        return updated or None


#: 🔴 Прежний `sendToDataBase` из `sys.modules` — чтобы вернуть его после теста.
#:
#: Дефект, найденный 12.10.2026. `_install` подменяет модуль `sendToDataBase`
#: урезанной заглушкой и **не убирает её**. Заглушка живёт в `sys.modules` до
#: конца процесса, поэтому **следующие файлы тестов** получали модуль без
#: `warehouse_id` и падали с `AttributeError` — а `test_telegram_offline.py`
#: вообще переста��ал получать сообщения (`IndexError: list index out of range`
#: в шести тестах).
#:
#: **Как это выглядело.** Каждый файл по отдельности проходил, а полный прогон
#: давал «6 failed». Это списывали на «проблему порядка тестов», которую можно не
#: чинить, — и тем самым **скрывали**, что команды `/offline` и `/online` в этих
#: тестах не проверялись вообще.
_ORIGINAL_SEND_TO_DB = sys.modules.get("sendToDataBase")


def _install(db: FakeDB) -> None:
    module = types.ModuleType("sendToDataBase")
    module.rest_get = db.rest_get
    module.rest_upsert = db.rest_upsert
    module.rest_delete = db.rest_delete
    module.rest_patch = db.rest_patch
    def _post(table, payload, **kwargs):
        # add_option пишет через rest_post: заглушка должна реально сохранять,
        # иначе тесты «проходят» на пустой базе и ничего не проверяют.
        # Справочнику `issue_templates` «база» выдаёт `id`, как identity-колонка:
        # без него создание заготовки вернуло бы `None` и связь не встала бы.
        rows = db.rows.setdefault(table, [])
        if table == storage.TEMPLATES_TABLE:
            rows.append({**dict(payload), "id": len(rows) + 1})
            return [rows[-1]]
        rows.append(dict(payload))
        return [payload]

    module.rest_post = _post
    sys.modules["sendToDataBase"] = module
    # storage проверяет готовность базы чтением колонки is_builtin
    storage.reset_capability_cache()
    # 🔴 **Кэш прочитанных строк узлов сбрасывается вместе с остальными.**
    #
    # Он живёт 1.5 секунды, чтобы одна сборка дерева не читала таблицу
    # двенадцать раз (замер 14.10.2026: 12 запросов, 6.5 с, из-за чего
    # `/intake/tree` в проде отвечал 12.7 с при таймауте API в 10 с). В тестах
    # между прогонами проходит меньше этого времени, поэтому без сброса
    # заглушка возвращал�� бы данные **прошлого** теста.
    storage.reset_node_rows_cache()
    storage._v2_state = True
    storage._v2_checked_at = float("inf")
    # Колонка связи `0109` в тестовой базе **есть**: без неё `link_option_to_template`
    # отказывал бы, и тесты связи проверяли бы не то.
    storage._link_state = True
    storage._link_checked_at = float("inf")
    # Пометка удаления (`0115`) в тестовой базе тоже есть: без этой строки
    # `delete_option` для встроенной кнопки отказывал бы «колонки нет», и тесты
    # удаления проверяли бы не то, что заявляют.
    storage._deleted_state = True
    storage._deleted_checked_at = float("inf")


def _uninstall() -> None:
    """Возвращает настоящий `sendToDataBase` — без этого течёт состояние.

    `None` значит, что модуль до теста **не был импортирован**: тогда е��о надо
    **удалить** из `sys.modules`, а не оставить заглушку.
    """
    if _ORIGINAL_SEND_TO_DB is None:
        sys.modules.pop("sendToDataBase", None)
    else:
        sys.modules["sendToDataBase"] = _ORIGINAL_SEND_TO_DB


class EditorStorageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.db = FakeDB()
        _install(self.db)
        self.addCleanup(storage.reset_capability_cache)
        # Возвращаем настоящий модуль сразу после теста — иначе заглушка утечёт
        # в следующие файлы (см. объяснение у `_ORIGINAL_SEND_TO_DB`).
        self.addCleanup(_uninstall)

    def _node_with_options(self):
        """Узел с вариантами, который **можно скрыть**.

        **Почему не любой узел с вариантами.** 07.10.2026 `robot_type` перестал
        подходить: его варианты ведут в **разные** узлы модулей
        (`robot_module_k50h` и `robot_module_a42t`), а такой узел скрывать
        нельзя — ссылки сошлись бы в одну и часть дерева потерялась бы (см.
        `hidden_node_target`). Первый узел в обходе оказался именно им, и тест
        падал, хотя защита сработала **правильно**.

        Здесь берётся узел, все варианты которого ведут в одно место, — на нём
        и проверяется сброс `hidden`.

        🔴 **И у которого вариантов БОЛЬШЕ ОДНОГО.** С 13.10.2026 последнюю
        видимую кнопку узла скрыть нельзя: узел-выбор без вариантов — тупик для
        оператора (осталась бы только «Cancel»), и это же отвергает валидатор
        дерева в боте. Первым в обходе идёт `robot_errors_chassis` с **одной**
        кнопкой, и тест падал на защите, работающей правильно. Условие «≥2»
        делает выбор узла честным: тест проверяет сброс `hidden`, а не границу
        защиты.
        """
        for node in DEFAULT_TREE.nodes.values():
            if not node.options or node.id == DEFAULT_TREE.root_id:
                continue
            # Меньше двух вариантов — скрывать нечего проверять: защита не даст.
            if len(node.options) < 2:
                continue
            targets = {option.next_node for option in node.options}
            targets.discard(None)
            if len(targets) == 1:
                return node
        self.fail("в дереве нет скрываемого узла с вариантами")

    # ---------- правка ВСТРОЕННОГО варианта ----------

    def test_edit_builtin_creates_override_not_new_row(self):
        node = self._node_with_options()
        option = node.options[0]

        ok = storage.update_option(node.id, option.id, label="Renamed", updated_by=7)

        self.assertTrue(ok)
        rows = self.db.rows["telegram_intake_options"]
        self.assertEqual(len(rows), 1, "правка встроенного должна дать одну строку-переопределение")
        self.assertEqual(rows[0]["option_id"], option.id)
        self.assertTrue(rows[0]["is_builtin"])
        self.assertEqual(rows[0]["label"], "Renamed")

    def test_effective_options_reflect_the_edit(self):
        node = self._node_with_options()
        option = node.options[0]
        storage.update_option(node.id, option.id, label="Renamed")

        items = storage.effective_options(DEFAULT_TREE, node.id, self.db.rows["telegram_intake_options"])

        got = next(i for i in items if i["id"] == option.id)
        self.assertEqual(got["label"], "Renamed")
        self.assertEqual(got["kind"], "edited")

    def test_edit_keeps_hidden_state(self):
        """Правка подписи не должна показывать скрытый вариант.

        `update_option` сохраняет текущее `hidden`, когда аргумент не передан —
        проверяем именно это, потому что рядом `update_node` так не делает
        (см. `test_update_node_does_not_reset_hidden`).
        """
        node = self._node_with_options()
        option = node.options[0]
        storage.set_option_hidden(node.id, option.id, True)

        storage.update_option(node.id, option.id, label="Renamed")

        rows = self.db.rows["telegram_intake_options"]
        row = next(r for r in rows if r["option_id"] == option.id)
        self.assertTrue(row["hidden"], "hidden сброшен правкой подписи")

    def test_edit_rejects_unknown_next_node(self):
        node = self._node_with_options()
        option = node.options[0]

        ok = storage.update_option(node.id, option.id, next_node="no_such_node")

        self.assertFalse(ok)

    def test_edit_rejects_empty_label(self):
        node = self._node_with_options()
        option = node.options[0]

        self.assertFalse(storage.update_option(node.id, option.id, label="   "))

    def test_edit_unknown_option_is_refused(self):
        node = self._node_with_options()

        self.assertFalse(storage.update_option(node.id, "no_such_option", label="X"))

    # ---------- порядок ----------

    def test_move_option_swaps_order(self):
        node = next(
            (n for n in DEFAULT_TREE.nodes.values() if len(n.options) >= 2),
            None,
        )
        if node is None:
            self.skipTest("нет узла с двумя вариантами")
        first, second = node.options[0].id, node.options[1].id

        ok = storage.move_option(node.id, second, "up")

        self.assertTrue(ok)
        items = storage.effective_options(DEFAULT_TREE, node.id, self.db.rows["telegram_intake_options"])
        order = [i["id"] for i in items]
        self.assertLess(order.index(second), order.index(first), "порядок не изменился")

    def test_move_option_beyond_edge_is_refused(self):
        node = self._node_with_options()
        self.assertFalse(storage.move_option(node.id, node.options[0].id, "up"))

    def test_move_option_rejects_bad_direction(self):
        node = self._node_with_options()
        self.assertFalse(storage.move_option(node.id, node.options[0].id, "sideways"))

    # ---------- удаление ----------

    def test_delete_removes_added_option(self):
        node = self._node_with_options()
        option_id = storage.add_option(node.id, "Added by test")
        self.assertTrue(option_id)

        self.assertTrue(storage.delete_option(node.id, option_id))
        rows = self.db.rows["telegram_intake_options"]
        self.assertFalse(any(r["option_id"] == option_id for r in rows))

    # ---------- узлы ----------

    def test_update_node_changes_title(self):
        node = self._node_with_options()

        ok = storage.update_node(node.id, title="New title", updated_by=1)

        self.assertTrue(ok)
        rows = self.db.rows["telegram_intake_nodes"]
        self.assertEqual(rows[0]["title"], "New title")

    def test_update_node_does_not_reset_hidden(self):
        """Скрытый узел не должен «показаться» от правки заголовка.

        Здесь ловится реальный дефект: `update_node` при непереданном `hidden`
        писал `false`, тогда как `update_option` в той же ситуации сохраняет
        текущее значение. Редактор правит заголовок без `hidden`
        (`editor.py`), поэтому скрытый узел молча становился видимым.
        """
        node = self._node_with_options()
        self.assertTrue(storage.update_node(node.id, hidden=True))
        rows = self.db.rows["telegram_intake_nodes"]
        self.assertTrue(next(r for r in rows if r["node_id"] == node.id)["hidden"])

        storage.update_node(node.id, title="Renamed")

        row = next(r for r in rows if r["node_id"] == node.id)
        self.assertTrue(row["hidden"], "правка заголовка сбросила hidden — узел снова виден")

    def test_editing_title_keeps_other_text_fields(self):
        """Правка одного поля не должна стирать соседние.

        Редактор правит по одному полю за раз (`editor.py`), поэтому раньше
        сохранение заголовка обнуляло описание, placeholder и подсказку — тот
        же класс дефекта, что и со `hidden`.
        """
        node = self._node_with_options()
        storage.update_node(
            node.id, title="T1", description="D1", placeholder="P1", stub_hint="S1"
        )

        storage.update_node(node.id, title="T2")

        row = storage.node_overrides()[node.id]
        self.assertEqual(row["title"], "T2")
        self.assertEqual(row["description"], "D1")
        self.assertEqual(row["placeholder"], "P1")
        self.assertEqual(row["stub_hint"], "S1")

    def test_explicit_empty_string_clears_the_field(self):
        """Очистка должна работать: пустая строка — это «стереть», а не «не трогать»."""
        node = self._node_with_options()
        storage.update_node(node.id, description="D1")

        storage.update_node(node.id, description="")

        row = storage.node_overrides()[node.id]
        self.assertFalse(row.get("description"), "описание не очистилось")

    def test_editing_description_keeps_title(self):
        node = self._node_with_options()
        storage.update_node(node.id, title="Keep me")

        storage.update_node(node.id, description="D2")

        row = storage.node_overrides()[node.id]
        self.assertEqual(row["title"], "Keep me")
        self.assertEqual(row["description"], "D2")

    def test_update_node_refuses_to_hide_root(self):
        self.assertFalse(storage.update_node(DEFAULT_TREE.root_id, hidden=True))

    def test_update_node_refuses_unknown_node(self):
        self.assertFalse(storage.update_node("no_such_node", title="X"))

    # ---------- выключенный полный режим ----------

    def test_missing_v2_columns_are_not_probed_on_every_call(self):
        """Отсутствие колонок v2 запоминается, а не проверяется заново каждый раз.

        Найдено по логам **живого прода**: без применённой миграции каждый
        вызов `load_option_rows` заново пробовал v2-колонки, получал 400 и
        `rest_get` писал ERROR. На каждое сообщение — строка ошибки, из-за
        которой настоящие проблемы не видно. Чтение работало (был откат на v1),
        но логи были непригодны.

        Проверяется число запросов, а не текст лога: так тест не зависит от
        формата сообщений.

        **Проб теперь две, и обе — один раз.** С 12.10.2026 добавляется набор со
        связью `template_id` (миграция `0109`): она приезжает **своей** миграцией
        и проверяется отдельно, чтобы её отсутствие не выглядело как «редактор не
        готов». Здесь важно, что обе пробы делаются на **первом** вызове, а дальше
        читается только v1.

        🔴 **С 13.10.2026 проб со `template_id` стало ДВЕ, и это не дефект.** Набор
        с пометкой удаления (`0115`) содержит `template_id`, поэтому считается
        этой же веткой счётчика: холодный вызов сначала пробует полный набор
        (`0115` + `0109`), затем набор без удаления, затем v2, затем v1. Каждая
        миграция — **своя** проба: PostgREST отвергает запрос целиком из-за одной
        неизвестной колонки, и общая проба означала бы, что неприехавшая `0115`
        снимает заодно связь с шаблонами.

        **Что здесь действительно важно:** число проб не растёт с числом вызовов.
        Поэтому проверка сравнивает холодный вызов и повторные, а не жёсткое «1»:
        иначе тест ловил бы не дефект кэша, а добавление новой миграции.
        """
        probes = {"link": 0, "v2": 0, "v1": 0}
        module = types.ModuleType("sendToDataBase")

        def rest_get(table, params=None, optional=False):
            if table == storage.OPTION_TABLE:
                select = (params or {}).get("select", "")
                if "template_id" in select:
                    probes["link"] += 1
                elif "is_builtin" in select:
                    probes["v2"] += 1
                else:
                    probes["v1"] += 1
            return None  # ни v2-колонок, ни v1-строк

        module.rest_get = rest_get
        module.rest_upsert = lambda *a, **k: [{}]
        module.rest_delete = lambda *a, **k: False
        module.rest_post = lambda *a, **k: [{}]
        sys.modules["sendToDataBase"] = module
        # Возвращаем настоящий модуль после теста: без этого заглушка
        # утекает в следующие файлы (см. `_ORIGINAL_SEND_TO_DB`).
        self.addCleanup(_uninstall)
        storage.reset_capability_cache()

        storage.load_option_rows()
        cold = dict(probes)

        for _ in range(4):
            storage.load_option_rows()

        # Пробы — только на холодном вызове.
        self.assertEqual(
            probes["v2"], cold["v2"], "колонки v2 проверяются заново на каждый вызов"
        )
        self.assertEqual(
            probes["link"], cold["link"], "проверка связи template_id идёт каждый раз"
        )
        # Холодный вызов укладывается в пробники: по одному на миграцию.
        self.assertLessEqual(cold["link"], 2, "лишние пробы на холодном вызове")
        self.assertGreaterEqual(cold["link"], 1, "связь обязана проверяться")
        # Откат к v1 — каждый раз: строки читаются по старому набору, пока
        # колонок v2 нет.
        self.assertEqual(probes["v1"], 5, "откат к v1 должен происходить каждый раз")

    def test_missing_nodes_table_is_not_queried_on_every_call(self):
        """Отсутствующая таблица правок узлов тоже запоминается.

        `load_node_rows` при отсутствии таблицы возвращает пустой список — это
        нормально, но сам запрос даёт 404 в логе. На каждое сообщение.

        🔴 **Первый вызов делает НЕСКОЛЬКО пробников — и это не дефект.** Набор
        колонок узла приезжает **тремя** миграциями: `0116` (тип узла и пометка
        удаления), `0114` (координаты холста) и `v2` (правки узлов). PostgREST
        отвергает запрос целиком из-за одной неизвестной колонки, поэтому
        отличить «нет таблицы» от «нет колонок типа» одним запросом нельзя: оба
        дают отказ. Пробники идут от полного набора к усечённому, и только после
        них состояние запоминается.

        **Что здесь действительно важно и что сторожит тест:** число запросов
        **не растёт с числом вызовов**. Проверка на точное «1» ловила бы не
        дефект кэша, а изменившееся число пробников, поэтому проверяются оба
        свойства: холодный вызов укладывается в пробники, а повторные — молчат.
        """
        queries = {"nodes": 0}
        module = types.ModuleType("sendToDataBase")

        def rest_get(table, params=None, optional=False):
            if table == storage.NODE_TABLE:
                queries["nodes"] += 1
            return None

        module.rest_get = rest_get
        module.rest_upsert = lambda *a, **k: [{}]
        module.rest_delete = lambda *a, **k: False
        module.rest_post = lambda *a, **k: [{}]
        sys.modules["sendToDataBase"] = module
        # Возвращаем настоящий модуль после теста: без этого заглушка
        # утекает в следующие файлы (см. `_ORIGINAL_SEND_TO_DB`).
        self.addCleanup(_uninstall)
        storage.reset_capability_cache()

        # Холодный вызов: по одному пробнику на миграцию (`0116` → `0114` →
        # v2 → v1). Каждая приезжает своей миграцией, и PostgREST отвергает
        # запрос целиком из-за одной неизвестной колонки — общая проба означала
        # бы, что неприехавшая `0116` снимает заодно координаты и правки.
        storage.load_node_rows()
        cold = queries["nodes"]

        self.assertLessEqual(cold, 4, "холодный вызов делает лишние пробники")
        self.assertGreaterEqual(cold, 1, "холодный вызов обязан спросить таблицу")

        # 🔴 Главное: повторные вызовы не добавляют запросов вовсе.
        for _ in range(4):
            storage.load_node_rows()

        self.assertEqual(
            queries["nodes"],
            cold,
            "отсутствующая таблица запрашивается каждый раз",
        )

    def test_full_edit_is_refused_when_migration_not_applied(self):
        """Без `sql/intake_editor_v2.sql` правки запрещены, а не «тихо сохранены»."""
        storage._v2_state = False
        node = self._node_with_options()

        self.assertFalse(storage.update_option(node.id, node.options[0].id, label="X"))
        self.assertFalse(storage.move_option(node.id, node.options[0].id, "down"))
        self.assertFalse(storage.update_node(node.id, title="X"))
        self.assertEqual(self.db.rows.get("telegram_intake_options", []), [])


class MissingMigrationNoiseTests(unittest.TestCase):
    """Отсутствие миграции v2 — ожидаемое состояние, а не сбой.

    Проверка колонок спрашивает `is_builtin`, которого до миграции нет, и
    PostgREST отвечает 400. Раньше этот запрос шёл **без** флага `optional`, и
    `rest_get` писал ERROR на каждую проверку — то есть пока админ открыт
    редактор, ошибка появлялась регулярно (раз в TTL), и настоящие проблемы
    тонули в шуме. Сообщение о состоянии остаётся, но на уровне WARNING.
    """

    def test_probe_does_not_log_an_error(self):
        import logging

        records = []

        class Handler(logging.Handler):
            def emit(self, record):
                records.append((record.levelname, record.getMessage()))

        handler = Handler()
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, handler)

        module = types.ModuleType("sendToDataBase")
        seen = {}

        def rest_get(table, params=None, optional=False):
            seen["optional"] = optional
            return None  # колонок v2 нет

        module.rest_get = rest_get
        module.rest_upsert = lambda *a, **k: [{}]
        module.rest_delete = lambda *a, **k: False
        module.rest_post = lambda *a, **k: [{}]
        sys.modules["sendToDataBase"] = module
        # Возвращаем настоящий модуль после теста: без этого заглушка
        # утекает в следующие файлы (см. `_ORIGINAL_SEND_TO_DB`).
        self.addCleanup(_uninstall)
        storage.reset_capability_cache()

        self.assertFalse(storage.overlay_supported())
        self.assertTrue(
            seen.get("optional"),
            "проверка колонок идёт без optional — в лог уйдёт ERROR",
        )

    def test_state_is_still_reported_as_warning(self):
        """Состояние не замалчивается: причина остаётся видимой."""
        import logging

        records = []

        class Handler(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Handler()
        handler.setLevel(logging.DEBUG)
        storage.logger.addHandler(handler)
        storage.logger.setLevel(logging.DEBUG)
        self.addCleanup(storage.logger.removeHandler, handler)

        module = types.ModuleType("sendToDataBase")
        module.rest_get = lambda *a, **k: None
        module.rest_upsert = lambda *a, **k: [{}]
        module.rest_delete = lambda *a, **k: False
        module.rest_post = lambda *a, **k: [{}]
        sys.modules["sendToDataBase"] = module
        # Возвращаем настоящий модуль после теста: без этого заглушка
        # утекает в следующие файлы (см. `_ORIGINAL_SEND_TO_DB`).
        self.addCleanup(_uninstall)
        storage.reset_capability_cache()

        storage.overlay_supported()

        self.assertTrue(
            any("intake_editor_v2" in m for m in records),
            "исчезло сообщение о том, что нужно применить миграцию",
        )


class DeleteAnyOptionChecks(EditorStorageTestCase):
    """
    Удаление **любой** кнопки и защита последней.

    Требование владельца 13.10.2026: «нужно чтобы полностью мог менять удалять
    кнопки все и переподвязывать их». Раньше удалить можно было только
    добавленную кнопку, а встроенную предлагалось скрыть.

    🔴 **Почему эти проверки живут на уровне `storage`, а не на ручке.** Первая
    версия проверяла только, что ручка вызывает `storage` — и **проходила даже
    со снятой защитой**: подменённая `delete_option` всегда отвечала успехом.
    То есть тест сторожил вызов, а не поведение. Здесь вызывается настоящая
    логика на базе в памяти, поэтому снятие защиты роняет проверку.
    """

    def _node_with_two_options(self):
        """Узел, у которого кнопок ≥2 — на нём удаление и скрытие разрешены."""
        for node in DEFAULT_TREE.nodes.values():
            if len(node.options) >= 2 and node.id != DEFAULT_TREE.root_id:
                return node
        self.fail("в дереве нет узла с двумя вариантами")

    def _node_with_one_option(self):
        """Узел ровно с одной кнопкой — на нём проверяется защита."""
        for node in DEFAULT_TREE.nodes.values():
            if len(node.options) == 1 and node.id != DEFAULT_TREE.root_id:
                return node
        self.fail("в дереве нет узла с одним вариантом")

    def test_builtin_option_is_marked_deleted(self):
        """🔴 Встроенная кнопка удаляется пометкой, а не удалением строки.

        Её определение лежит в коде бота: удаление строки вернуло бы кнопку к
        исходному виду, и «удаление» выглядело бы неработающим.
        """
        node = self._node_with_two_options()
        option = node.options[0]

        self.assertTrue(
            storage.delete_option(node.id, option.id),
            "встроенную кнопку должно быть можно удалить",
        )

        row = next(
            r for r in self.db.rows["telegram_intake_options"]
            if r["node_id"] == node.id and r["option_id"] == option.id
        )
        self.assertTrue(row["deleted"], "пометки удаления нет — кнопка останется в дереве")

        # И она действительно исчезает из рабочего дерева.
        item = next(
            i for i in storage.effective_options(DEFAULT_TREE, node.id, self.db.rows["telegram_intake_options"])
            if i["id"] == option.id
        )
        self.assertTrue(item["deleted"])

    def test_restore_brings_the_button_back(self):
        """Удаление обратимо: `restore_option` возвращает кнопку."""
        node = self._node_with_two_options()
        option = node.options[0]

        storage.delete_option(node.id, option.id)
        self.assertTrue(storage.restore_option(node.id, option.id))

        item = next(
            i for i in storage.effective_options(DEFAULT_TREE, node.id, self.db.rows["telegram_intake_options"])
            if i["id"] == option.id
        )
        self.assertFalse(item["deleted"], "кнопка не вернулась из удаления")

    def test_added_option_is_removed_from_the_table(self):
        """Добавленная кнопка живёт только строкой — её убираем совсем."""
        node = self._node_with_two_options()
        created = storage.add_option(node.id, "Temp added")

        self.assertTrue(storage.delete_option(node.id, created))

        left = [
            r for r in self.db.rows["telegram_intake_options"]
            if r["node_id"] == node.id and r["option_id"] == created
        ]
        self.assertEqual(left, [], "строка добавленной кнопки осталась в базе")

    def test_last_visible_option_cannot_be_deleted(self):
        """🔴 Последнюю кнопку узла удалить нельзя — узел стал бы тупиком.

        Без вариантов бот показал бы оператору только «Cancel», и записать
        ошибку было бы нечем. Это же отвергает валидатор дерева
        (`types.py::validate`: `choice` без вариантов — ошибка).
        """
        node = self._node_with_one_option()

        self.assertFalse(
            storage.delete_option(node.id, node.options[0].id),
            "последняя кнопка удалена — оператор упёрся бы в тупик",
        )
        self.assertEqual(
            storage.visible_options_count(node.id), 1, "кнопка пропала из дерева"
        )

    def test_last_visible_option_cannot_be_hidden(self):
        """🔴 Скрыть последнюю кнопку тоже нельзя: тупик тот же.

        Проверка стоит на **обоих** путях намеренно: удаление и скрытие ведут к
        одному состоянию («у узла нет кнопок»), и защита на одном из них
        оставила бы второй открытым.
        """
        node = self._node_with_one_option()

        self.assertFalse(
            storage.set_option_hidden(node.id, node.options[0].id, True),
            "последняя кнопка скрыта — оператор упёрся бы в тупик",
        )
        self.assertEqual(storage.visible_options_count(node.id), 1)

    def test_hiding_a_non_last_option_still_works(self):
        """Защита не мешает обычному скрытию, когда кнопок больше одной."""
        node = self._node_with_two_options()
        option = node.options[0]

        self.assertTrue(storage.set_option_hidden(node.id, option.id, True))
        # И обратно показывается.
        self.assertTrue(storage.set_option_hidden(node.id, option.id, False))

    def test_deleting_twice_is_not_an_error(self):
        """Повторное удаление — тот же результат, а не отказ.

        Иначе повторное нажатие (или две вкладки) показывало бы ошибку на уже
        достигнутом состоянии.
        """
        node = self._node_with_two_options()
        option = node.options[0]

        self.assertTrue(storage.delete_option(node.id, option.id))
        self.assertTrue(storage.delete_option(node.id, option.id))


class CauseScopeChecks(EditorStorageTestCase):
    """
    Причины привязаны к модулю **самим узлом**, а не фильтром `only_for`.

    Требование владельца 13.10.2026: «сделай Block What caused it? на каждый
    модуль». Раньше узел причин был один (`ask_cause`), а причины ограничивались
    по модулям полем `only_for` — и это требовало сторожа «не оставить модуль без
    причин». Теперь у каждого модуля **свой узел** со своим списком, и сторож не
    нужен: пустой список невозможен по построению (см. `test_module_tree.py`).

    🔴 **Почему проверки на уровне `storage`.** Здесь проверяется то, что видит
    оператор: кнопка ошибки ведёт в узел причин своего модуля и **не** в чужой.
    """

    def test_error_buttons_lead_to_their_own_module_cause_node(self):
        """
        Кнопка ошибки ведёт в узел причин **своего** модуля.

        Дефект, который это ловит: все ошибки вели в один общий `ask_cause`, и
        оператор на шасси видел причины камеры.
        """
        for module in MODULE_ERRORS:
            node = DEFAULT_TREE.node(error_node_id(module))
            expected = cause_node_id(module)

            for option in node.options:
                self.assertEqual(
                    option.next_node,
                    expected,
                    f"{module}: ошибка {option.id} ведёт не в свой узел причин",
                )

    def test_cause_lists_differ_between_modules(self):
        """Списки причин у модулей разные — иначе разделение бессмысленно."""
        chassis = tuple(o.id for o in DEFAULT_TREE.node(cause_node_id("chassis")).options)
        qr = tuple(o.id for o in DEFAULT_TREE.node(cause_node_id("qr_code")).options)

        self.assertNotEqual(chassis, qr)
        # «Код места повреждён» бывает только у QR.
        self.assertIn("dirty_code", qr)
        self.assertNotIn("dirty_code", chassis)

    def test_template_link_is_per_node_not_per_option_id(self):
        """
        🔴 Связь заготовки различается по узлу, а не только по `option_id`.

        Это **дефект, найденный при разделении**: карта связей ключевалась одним
        `option_id`, а id причин повторяются (`human_error` есть у всех модулей).
        Связь, выставленная на причине шасси, «уехала» бы на причину подъёма —
        проверено запуском: обе ветки получали один и тот же `template_id`.

        Теперь связь хранится с узлом, и старая схема ключа остаётся запасной —
        у ошибок (`robot_errors_*`) id уникальны, и уже сохранённые связи
        работают без миграции.
        """
        from equipment_intake import error_templates as et

        templates = [
            {"id": 10, "employee_title": "Human error chassis"},
            {"id": 20, "employee_title": "Human error lift"},
        ]
        # Связь выставлена ТОЛЬКО у причины шасси.
        links = {"ask_cause_chassis::human_error": 10}

        chassis_steps = [
            {"node_id": "robot_errors_chassis", "option_id": "chassis_went_off_road", "label": "Went off the road"},
            {"node_id": "ask_cause_chassis", "option_id": "human_error", "label": "Human error"},
        ]
        lift_steps = [
            {"node_id": "robot_errors_lift", "option_id": "lift_cant_lift", "label": "Can't lift"},
            {"node_id": "ask_cause_lift", "option_id": "human_error", "label": "Human error"},
        ]

        found = et.linked_template(chassis_steps, templates, links)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], 10)

        # У подъёма связи нет — и он НЕ получает связь шасси.
        self.assertIsNone(
            et.linked_template(lift_steps, templates, links),
            "связь причины шасси применилась к причине подъёма",
        )

    def test_old_option_id_key_still_works_for_errors(self):
        """
        Старая схема ключа (`option_id`) продолжает работать для ошибок.

        Совместимость обязательна: связи ошибок уже сохранены в базе этой схемой,
        и ломать их нельзя — иначе у владельца «пропали» бы заготовки.
        """
        from equipment_intake import error_templates as et

        templates = [{"id": 10, "employee_title": "Went off the road"}]
        steps = [
            {"node_id": "robot_errors_chassis", "option_id": "chassis_went_off_road", "label": "Went off the road"}
        ]

        found = et.linked_template(steps, templates, {"chassis_went_off_road": 10})

        self.assertIsNotNone(found, "связь ошибки по старому ключу перестала работать")
        self.assertEqual(found["id"], 10)


class FailureNoticeTests(unittest.TestCase):
    """Отказ правки обязан называть причину, а не «не изменилось».

    Раньше при неприменённой миграции админ видел «⚠️ Option not changed.» —
    выглядело как сбой бота, а не как неприменённая миграция.
    """

    def setUp(self) -> None:
        self.addCleanup(storage.reset_capability_cache)

    def test_success_is_shown_as_is(self):
        from equipment_intake import editor

        self.assertEqual(editor._failure_notice(True, "✅ Done."), "✅ Done.")

    def test_missing_migration_is_named_as_the_reason(self):
        from equipment_intake import editor

        storage.reset_capability_cache()
        storage._remember_v2(False)

        notice = editor._failure_notice(False, "✅ Done.")

        self.assertIn("intake_editor_v2", notice, "причина не названа")
        self.assertIn("is_builtin", notice, "не сказано, чего не хватает")

    def test_other_rejection_stays_generic(self):
        """Когда схема готова, причина другая — выдумывать её нельзя."""
        from equipment_intake import editor

        storage.reset_capability_cache()
        storage._remember_v2(True)

        notice = editor._failure_notice(False, "✅ Done.")

        self.assertNotIn("intake_editor_v2", notice)
        self.assertIn("rejected", notice)


class TemplateLinkStorageChecks(EditorStorageTestCase):
    """Заготовка и связь ошибки дерева со справочником (`issue_templates`).

    Требование владельца 12.10.2026: «при добавление новой ошибки делать
    заготовку тоже чтобы потом были поля которые будут подставлены
    автоматически». Здесь проверяется, что заготовка **создаётся**, что поля
    правятся по одному и что связь **не теряется** при правке варианта.
    """

    def _error_node(self, minimum: int = 1):
        """Узел слоя ошибок — на нём связь и проверяется.

        У встроенного варианта нет строки-оверлея, поэтому связь создаётся
        первым же `link_option_to_template` — это и нужно.

        `minimum` — сколько вариантов требуется: у узла шасси он один, а для
        проверок «первый/второй» нужен узел с несколькими (`robot_errors_safety`).
        """
        for node_id, node in DEFAULT_TREE.nodes.items():
            if node_id.startswith("robot_errors_") and len(node.options) >= minimum:
                return node

        self.fail(f"в дереве нет узла слоя ошибок с {minimum}+ вариантами")

    def test_stub_row_keeps_unknown_fields_empty(self):
        """Создание заготовки: описание и модуль есть, «что было сделано» — нет."""
        template_id = storage.create_template(
            "Conveyor jammed", module="Chassis", created_by=42
        )

        self.assertIsNotNone(template_id)

        row = self.db.rows[storage.TEMPLATES_TABLE][0]

        self.assertEqual(row["employee_title"], "Conveyor jammed")
        self.assertEqual(row["issue_type"], "Chassis")
        self.assertEqual(row["issue_description"], "Conveyor jammed")
        self.assertEqual(row["equipment_type"], "Equipment")
        self.assertIsNone(row["recovery_title"])
        self.assertIsNone(row["solving_time"])

    def test_create_refuses_empty_title(self):
        """Без названия заготовка не создаётся: строка-пустышка смысла не имеет."""
        self.assertIsNone(storage.create_template("   "))
        self.assertEqual(self.db.rows.get(storage.TEMPLATES_TABLE, []), [])

    def test_link_is_written_and_readable(self):
        node = self._error_node()
        option = node.options[0]

        linked = storage.link_option_to_template(node.id, option.id, 152, updated_by=1)

        self.assertTrue(linked)
        self.assertEqual(storage.option_template_id(node.id, option.id), 152)

    def test_link_map_only_contains_linked_options(self):
        """Карта связей, которую читает писатель журнала."""
        node = self._error_node(minimum=2)
        first, second = node.options[0], node.options[1]

        storage.link_option_to_template(node.id, first.id, 152)
        storage.link_option_to_template(node.id, second.id, 156)
        storage.reset_links_cache()

        links = storage.option_template_links()

        self.assertEqual(links.get(first.id), 152)
        self.assertEqual(links.get(second.id), 156)
        self.assertNotIn("no_such_option", links)

    def test_editing_another_field_does_not_drop_the_link(self):
        """🔴 Переименование варианта не должно ронять связь.

        Связь держится на `merge-duplicates`: `rest_upsert` обновляет **только
        колонки из payload**, и незаписанный `template_id` остаётся прежним.
        Дефект был бы тихим: оператор увидел бы в журнале пустое «что было
        сделано», а в логах не было бы ни строки.

        **Негативная проверка (выполнена):** добавить в строку `update_option`
        `"template_id": None` — тест падает. Именно так это и ломается: соблазн
        «записать строку целиком» уже приводил к затиранию соседних полей у
        узлов (см. `update_node`).
        """
        node = self._error_node()
        option = node.options[0]

        storage.link_option_to_template(node.id, option.id, 152)
        renamed = storage.update_option(
            node.id, option.id, label="Renamed error", updated_by=1
        )

        self.assertTrue(renamed)
        self.assertEqual(
            storage.option_template_id(node.id, option.id), 152,
            "связь со шаблоном потеряна при переименовании",
        )

    def test_moving_an_option_does_not_drop_the_link(self):
        """Перемещение «выше/ниже» тоже не должно ронять связь.

        Берётся узел с **несколькими** вариантами (`robot_errors_safety`): у узла
        шасси он один, и тест на нём молча скипался бы — то есть проверка
        «перемещение не роняет связь» не выполнялась бы никогда.
        """
        node = self._error_node(minimum=2)
        second = node.options[1]

        storage.link_option_to_template(node.id, second.id, 156)
        moved = storage.move_option(node.id, second.id, "up", updated_by=1)

        self.assertTrue(moved)
        self.assertEqual(storage.option_template_id(node.id, second.id), 156)

    def test_template_fields_are_updated_one_by_one(self):
        """Поля заготовки правятся по одному, не затирая соседние.

        Это требование владельца: «на сервере в столбцы с описанием ошибки что
        было сделано и тп». Форма правит по полю за раз, и запись всего объекта
        стёрла бы уже заполненное.
        """
        template_id = storage.create_template("Went off the road", module="Chassis")
        row = self.db.rows[storage.TEMPLATES_TABLE][0]

        self.assertTrue(storage.update_template(template_id, recovery="Move the robot"))
        self.assertEqual(row["recovery_title"], "Move the robot")
        self.assertEqual(row["issue_description"], "Went off the road")

        self.assertTrue(storage.update_template(template_id, solving_time=6))
        self.assertEqual(row["solving_time"], 6)
        self.assertEqual(row["recovery_title"], "Move the robot")

        self.assertTrue(storage.update_template(template_id, description="Robot off road"))
        self.assertEqual(row["issue_description"], "Robot off road")
        self.assertEqual(row["solving_time"], 6)

    def test_template_description_edit_keeps_the_title(self):
        """Правка описания не переименовывает заготовку."""
        template_id = storage.create_template("QR Code dirty", module="QR Code")
        row = self.db.rows[storage.TEMPLATES_TABLE][0]

        storage.update_template(template_id, description="DM code is unreadable")

        self.assertEqual(row["employee_title"], "QR Code dirty")

    def test_bad_solving_time_does_not_break_the_save(self):
        """Мусор во времени решения приводится к нулю, а не роняет сохранение."""
        template_id = storage.create_template("Safety bumper trigger", module="Safety")

        self.assertTrue(storage.update_template(template_id, solving_time="abc"))
        self.assertEqual(self.db.rows[storage.TEMPLATES_TABLE][0]["solving_time"], 0)

        self.assertTrue(storage.update_template(template_id, solving_time="8"))
        self.assertEqual(self.db.rows[storage.TEMPLATES_TABLE][0]["solving_time"], 8)

    def test_update_without_a_template_id_is_refused(self):
        """Правка без id — отказ, а не запись «во все строки»."""
        self.assertFalse(storage.update_template(None, recovery="x"))
        self.assertFalse(storage.update_template("", recovery="x"))

    def test_link_requires_the_column(self):
        """Без применённой миграции `0109` связь не пишется и не молчит.

        `link_option_to_template` обязан вернуть `False`, а не сделать вид, что
        сохранил: иначе админ увидел бы «связано», а связи в базе нет.
        """
        node = self._error_node()

        storage.reset_capability_cache()
        storage._remember_link(False)

        self.assertFalse(
            storage.link_option_to_template(node.id, node.options[0].id, 152)
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)


class NewModuleGetsItsOwnErrorAndCauseNodes(EditorStorageTestCase):
    """
    🔴 **Добавил модуль — блоков `robot_errors_…` и причин хватает.**

    Требование владельца 13.10.2026, дословно: «окей я если я допустим добавил
    новый модуль на робота и теперь хочу добавить robots_error_что то там».

    **Что ломалось.** id нового блока выводился **только из заголовка**, а узлы
    ошибок и причин связаны **именами**: `robot_errors_<модуль>` →
    `ask_cause_<модуль>`. Заголовок «Fingers: what is wrong?» давал
    `fingers_what_is_wrong`, и причины для нового модуля не находились.

    Воспроизведено на **живой ветке владельца**: у созданного им
    `robot_errors_fingers` единственная ошибка вела в `ask_cause_qr_code`, и
    оператор, выбрав «Fingers stuck open/close», видел причины QR-кода
    («Dirty or damaged floor code»). Это не косметика: причина уходит в отчёт и
    определяет метрику по ошибкам.
    """

    def test_add_node_creates_the_contract_id(self):
        """
        🔴 **Сквозная проверка: `add_node` создаёт узел с контрактным id.**

        ⚠️ **Почему именно так, а не только проверка `_node_id_for_insertion`.**
        Первая версия этих тестов звала `_node_id_for_insertion` **напрямую**, и
        негативная проверка её не поймала: сломанным оказался вызов внутри
        `add_node` (id по-прежнему брался из заголовка), а сама функция
        оставалась целой. Тест обязан идти через **тот же путь**, что и веб.

        Модуль `camera` выбран потому, что узла `robot_errors_camera` в дереве
        **нет** — иначе проверка не отличила бы «id выведен из места вставки» от
        «узел уже существовал» (на этом упала соседняя проверка про `tray`).
        """
        # Кнопка модуля, которую вставляет веб перед созданием узла ошибок.
        # `add_option` берёт **подпись**, а `option_id` выводит из неё.
        option_id = storage.add_option(
            "robot_module_a42t", "Camera",
            next_node="ask_number", created_by="test",
        )
        self.assertEqual(option_id, "camera", "id кнопки модуля не тот")
        self.addCleanup(
            storage.delete_option, "robot_module_a42t", option_id, updated_by="cleanup"
        )

        node_id = storage.add_node(
            title="Camera: what is wrong?",
            node_type="choice",
            after_node_id="robot_module_a42t",
            after_option_id="camera",
            created_by="test",
        )

        self.assertIsNotNone(node_id, "узел не создался")
        self.addCleanup(storage.delete_node, node_id, updated_by="cleanup")

        self.assertEqual(
            node_id, "robot_errors_camera",
            "id узла ошибок не следует контракту имён",
        )

    def test_add_node_gives_the_cause_node_its_contract_id(self):
        """
        🔴 Сквозная проверка узла **причин**: вставка за кнопкой ошибки.

        Это исправление ветки `Fingers` владельца: причина обязана быть своей
        (`ask_cause_…`), а не вести в причины чужого модуля.
        """
        option_id = storage.add_option(
            "robot_module_a42t", "Camera",
            next_node="ask_number", created_by="test",
        )
        self.addCleanup(
            storage.delete_option, "robot_module_a42t", option_id, updated_by="cleanup"
        )

        module = storage.add_node(
            title="Camera: what is wrong?",
            node_type="choice",
            after_node_id="robot_module_a42t",
            after_option_id=option_id,
            created_by="test",
        )

        self.assertEqual(module, "robot_errors_camera")
        self.addCleanup(storage.delete_node, module, updated_by="cleanup")

        error_id = storage.add_option(
            module, "Camera is dead", next_node="ask_number", created_by="test",
        )
        self.addCleanup(storage.delete_option, module, error_id, updated_by="cleanup")

        cause = storage.add_node(
            title="Why did it happen?",
            node_type="choice",
            after_node_id=module,
            after_option_id=error_id,
            created_by="test",
        )

        self.assertIsNotNone(cause, "узел причин не создался")
        self.addCleanup(storage.delete_node, cause, updated_by="cleanup")

        self.assertEqual(
            cause, "ask_cause_camera",
            "id узла причин не следует контракту имён",
        )

    def test_error_node_gets_the_contract_id(self):
        """Вставка за кнопкой модуля даёт `robot_errors_<хвост>`."""
        node_id = storage._node_id_for_insertion(
            "Camera: what is wrong?", storage._known_node_ids(DEFAULT_TREE),
            after_node_id="robot_module_a42t", after_option_id="camera",
        )

        self.assertEqual(node_id, "robot_errors_camera")

    def test_module_tail_follows_the_map_not_the_label(self):
        """
        Хвост берётся из карты модулей: `Lifting` у K50H → `lift`, не `lifting`.

        У K50H модуль подписан `Lifting`, а узел ошибок — `robot_errors_lift`.
        Слепое `error_node_id("lifting")` создало бы **второй** узел для того же
        железа, и правки разошлись бы.

        ⚠️ **Проверяется сама карта, а не создание узла.** `robot_errors_lift`
        уже есть в дереве, поэтому `_node_id_for_insertion` законно вернул бы
        id из заголовка (второй узел не создаётся) — и тест не отличил бы
        «хвост взят из карты» от «узел уже был». Первая версия этой проверки
        именно на этом и упала.
        """
        from equipment_intake.tree_config import module_tail_for_option

        self.assertEqual(
            module_tail_for_option("robot_module_k50h", "lifting"), "lift"
        )
        # Модуль, добавленный из веба, в карте отсутствует -> хвост из option_id.
        self.assertEqual(
            module_tail_for_option("robot_module_a42t", "fingers"), "fingers"
        )

    def test_cause_node_gets_the_contract_id(self):
        """
        Вставка за кнопкой **узла ошибок** даёт узел причин того же модуля.

        Это и есть исправление ветки `Fingers`: причина обязана быть своей.
        """
        node_id = storage._node_id_for_insertion(
            "Why did it happen?", storage._known_node_ids(DEFAULT_TREE),
            after_node_id="robot_errors_fingers",
            after_option_id="fingers_stuck_open_close",
        )

        self.assertEqual(node_id, "ask_cause_fingers")

    def test_explicit_title_still_wins(self):
        """Явный заголовок по контракту уважается — прежние блоки не ломаются."""
        node_id = storage._node_id_for_insertion(
            "robot_errors_camera", storage._known_node_ids(DEFAULT_TREE),
            after_node_id="robot_module_a42t", after_option_id="camera",
        )

        self.assertEqual(node_id, "robot_errors_camera")

    def test_ordinary_block_keeps_the_old_behaviour(self):
        """Обычная вставка «за кнопкой» работает как раньше — id из заголовка."""
        node_id = storage._node_id_for_insertion(
            "Which cable is damaged?", storage._known_node_ids(DEFAULT_TREE),
            after_node_id="root", after_option_id="robot",
        )

        self.assertEqual(node_id, "which_cable_is_damaged")

    def test_existing_error_node_is_not_duplicated(self):
        """
        Если узел ошибок модуля **уже есть**, второй не создаётся.

        Живой случай: владелец сам создал `robot_errors_fingers` из веба. Вставка
        «ещё одного» дала бы два узла на один модуль, и правки разошлись бы —
        ровно то, от чего предостерегает `cause_node_id`.
        """
        known = storage._known_node_ids(DEFAULT_TREE)

        # Берётся узел **из кода** (`robot_errors_tray`): он есть в дереве
        # всегда. `robot_errors_fingers` создан владельцем в живой базе, и в
        # офлайновой подмене его нет — проверка на нём падала бы не по делу.
        self.assertIn("robot_errors_tray", known, "нет узла для проверки")

        node_id = storage._node_id_for_insertion(
            "Tray: what is wrong?", known,
            after_node_id="robot_module_a42t", after_option_id="tray",
        )

        self.assertNotEqual(
            node_id, "robot_errors_tray",
            "второй узел ошибок для того же модуля",
        )


class NodeRowsAreCachedButNeverStale(EditorStorageTestCase):
    """
    🔴 **Строки узлов читаются один раз на сборку дерева — и не устаревают.**

    Дефект, найденный замером 14.10.2026. `apply_overlay()` делал **12 запросов**
    к `telegram_intake_nodes` — по одному на каждый узел: `_web_node` зовётся из
    `_known_node_ids` и из `effective_options`. В сумме это **6.5 секунды**, а
    `/intake/tree` в проде отвечал **12.7 с** при таймауте API в 10 с — карта
    дерева не открывалась вовсе, с текстом «Intake bot is unreachable» на живом
    боте.

    Лечится коротким кэшем, но у него есть **вторая сторона**: правка из веба
    обязана быть видна сразу. Поэтому кэш сбрасывается при **каждой** записи
    узла, а TTL (1.5 с) остаётся лишь вторым рубежом. Оба свойства проверяются
    здесь: без первого карта снова станет медленной, без второго — покажет
    прежнее состояние после сохранения.
    """

    def _count_reads(self) -> dict:
        """Ставит счётчик поверх рабочей заглушки этого класса.

        Обёртка, а не своя заглушка: своя вернула бы пустой список, и тест
        проверял бы не кэш, а отсутствие данных. Здесь `_install` уже подменил
        `sendToDataBase`, и мы считаем **реальные** обращения к таблице узлов.
        """
        import sendToDataBase as db

        counter = {"nodes": 0}
        real_get = db.rest_get

        def counting_get(table, params=None, optional=False):
            if table == storage.NODE_TABLE:
                counter["nodes"] += 1
            return real_get(table, params, optional)

        db.rest_get = counting_get
        self.addCleanup(lambda: setattr(db, "rest_get", real_get))

        return counter

    def test_repeated_reads_do_not_hit_the_table(self):
        """Повторные чтения не добавляют запросов — иначе смысл кэша теряется."""
        counter = self._count_reads()

        storage.reset_node_rows_cache()
        storage.load_node_rows()
        first = counter["nodes"]

        for _ in range(5):
            storage.load_node_rows()

        self.assertEqual(
            counter["nodes"],
            first,
            "повторное чтение снова спросило таблицу — кэш не работает",
        )
        self.assertGreater(first, 0, "холодное чтение о��язано спросить таблицу")

    def test_writing_a_node_makes_the_next_read_fresh(self):
        """
        🔴 **Запись узла сбрасывает кэш — правка видна сразу.**

        Без сброса карта после сохранения показала бы прежнее состояние, и
        человек решил бы, что правка не применилась: это ровно тот класс
        дефекта, который уже ловили на «ветка не в работе».
        """
        counter = self._count_reads()

        # Прогреваем кэш.
        storage.reset_node_rows_cache()
        storage.load_node_rows()
        before = counter["nodes"]

        node_id = storage.add_node(
            title="Camera: what is wrong?",
            node_type="choice",
            after_node_id="root",
            after_option_id="robot",
            created_by="test",
        )
        self.assertIsNotNone(node_id, "узел не создался")
        self.addCleanup(storage.delete_node, node_id, updated_by="cleanup")

        # Следующее чтение обязано увидеть новый узел, а не закэшированный ответ.
        rows = storage.load_node_rows() or []
        ids = {str(row.get("node_id")) for row in rows}

        self.assertIn(node_id, ids, "новый узел не виден сразу после создания")
        self.assertGreater(
            counter["nodes"],
            before,
            "после записи чтение не спросило таблицу — кэш не сброшен",
        )

    def test_position_write_also_refreshes(self):
        """Правка позиции — тоже запись: кэш сбрасывается и после неё."""
        counter = self._count_reads()

        storage.reset_node_rows_cache()
        storage.load_node_rows()
        before = counter["nodes"]

        self.assertTrue(
            storage.update_node_position("root", 111.5, 222.5, updated_by="test"),
            "позиция не записалась",
        )
        storage.load_node_rows()

        self.assertGreater(
            counter["nodes"], before, "кэш не сброшен после записи позиции"
        )


class NoLoopsAndNoSilentDataLoss(EditorStorageTestCase):
    """
    🔴 Правки из веба не должны создавать петли и молча терять данные.

    Четыре дефекта, найденные 13.10.2026 при аудите. У всех одна природа:
    **запись отвечала «получилось» там, где получалось неверное дерево или
    потеря уже сохранённого**. Снаружи это выглядело как успешное действие.
    """

    def test_option_cannot_lead_back_to_its_own_node(self):
        """
        Петля: опция ведёт на **свой же** узел — оператор ходит по кругу.

        Воспроизведено на живых данных: `update_option("ask_cause_tray",
        "overload", next_node="ask_cause_tray")` возвращал `True`, и обход давал
        глубину 6, 7, 8, 9 — без конца. Отчёт при этом **не писался**, а выйти
        можно было только кнопкой Cancel.
        """
        self.assertFalse(
            storage.update_option(
                "ask_cause_tray", "overload",
                next_node="ask_cause_tray", updated_by="test",
            ),
            "петля на свой узел принята — оператор зациклится",
        )

        # И нормальная цель по-прежнему проходит: проверка не должна ломать
        # обычную работу.
        self.assertTrue(
            storage.update_option(
                "ask_cause_tray", "overload", next_node="summary", updated_by="test"
            )
        )

    def test_new_option_cannot_lead_back_to_its_own_node(self):
        """
        Тот же случай, но через **создание** кнопки.

        `add_option` — отдельный путь записи: проверка только в `update_option`
        оставляла эту дверь открытой, и петля создавалась одним вызовом.
        """
        self.assertIsNone(
            storage.add_option(
                "ask_cause_tray", "Loop", next_node="ask_cause_tray", created_by="test"
            ),
            "новая кнопка с петлёй создана",
        )

    def test_garbage_coordinate_does_not_erase_the_position(self):
        """
        🔴 Мусор в координате — отказ, а **не** «сброс в авто-раскладку».

        `_clean_position` возвращает `None` и для мусора (`"abc"`), и для
        осмысленного «вернуть блок в авто-раскладку». Пока запись опиралась
        только на неё, опечатка **стирала расстановку молча**: на живой базе
        блок `root` потерял `(-977.18, 459.31)` после вызова с `"abc"`.
        """
        self.assertFalse(
            storage.update_node_position("root", "abc", "xyz", updated_by="test"),
            "мусор в координате принят — позиция блока стёрта молча",
        )

        # Осмысленные случаи обязаны работать: сброс и настоящие числа.
        self.assertTrue(
            storage.update_node_position("root", None, None, updated_by="test"),
            "«вернуть в авто-раскладку» перестало работать",
        )
        self.assertTrue(
            storage.update_node_position("root", 10.5, 20.25, updated_by="test")
        )

    def test_nan_coordinate_is_rejected(self):
        """
        `NaN` — тоже мусор.

        Он проходит сравнения (`nan != nan` — единственное исключение), поэтому
        проверяется отдельно: без этого `NaN` попал бы в базу и сломал и
        раскладку, и `JSON` в ответе API.
        """
        self.assertFalse(
            storage.update_node_position("root", float("nan"), 1, updated_by="test"),
            "NaN принят как координата",
        )

    def test_link_to_a_missing_template_is_rejected(self):
        """
        🔴 Ссылка на **несуществующую** заготовку не выставляется.

        Воспроизведено: `link_option_to_template(..., 999999)` отвечал `True`.
        Человек видел «связь ��сть», а в отчёт **ничего не подставлялось** — тип
        ошибки, «что сделано» и время решения оставались пустыми, и понять
        причину по интерфейсу было нельзя.
        """
        self.db.rows[storage.TEMPLATES_TABLE] = [
            {"id": 38, "employee_title": "Equipment"},
        ]

        self.assertFalse(
            storage.link_option_to_template(
                "ask_cause_tray", "overload", 999999, updated_by="test"
            ),
            "связь с несуществующей заготовкой выставлена",
        )
        self.assertTrue(
            storage.link_option_to_template(
                "ask_cause_tray", "overload", 38, updated_by="test"
            ),
            "связь с существующей заготовкой не выставилась",
        )
        # `None` — осмысленное «снять связь», его проверять нечем.
        self.assertTrue(
            storage.link_option_to_template(
                "ask_cause_tray", "overload", None, updated_by="test"
            )
        )

    def test_link_still_works_when_the_catalogue_is_unreachable(self):
        """
        Обратная сторона: **пустой справочник не запрещает связь.**

        `load_templates` отдаёт пустой список и для «заготовок нет», и для
        «база недоступна». Отвергать связь на пустом списке значило бы запретить
        привязку при сбое чтения — человек не понял бы, почему «не сохраняется».
       """
        self.db.rows[storage.TEMPLATES_TABLE] = []

        self.assertTrue(
            storage.link_option_to_template(
                "ask_cause_tray", "overload", 38, updated_by="test"
            ),
            "связь запрещена из-за недоступного справочника — мягкая деградация сломана",
        )


class WebButtonStaysAnAddition(EditorStorageTestCase):
    """
    🔴 **Кнопка из веба остаётся «добавленной», а не «правкой встроенной».**

    Живой дефект 13.10.2026. Кнопка `robot_errors_fingers
    .fingers_stuck_open_close` создана из веба — в коде её нет. После привязки
    заготовки она получила `is_builtin=true`.

    **Чем это видно человеку.** `effective_options` считает такую строку
    «правкой встроенного варианта» и не отдаёт её как добавленную, поэтому
    **карта веба показывала у блока 0 кнопок**, тогда как рабочее дерево — 1.
    Владелец видел пустой блок и не мог привязать к нему заготовку.

    **Причина.** `link_option_to_template` брал `is_builtin` из словаря
    `effective_options`, собранного по **базовому** дереву: встроенной кнопки
    там нет, и признак выходил `True`. Теперь он берётся по факту —
    `_find_base_option`.
    """

    def _web_error_button(self) -> tuple[str, str]:
        """Кнопка ошибки, созданная из веба (в коде её нет)."""
        # Якорь — кнопка, которая есть в **коде** (`root.robot`): в офлайновой
        # заглушке живых строк базы нет, и кнопку из веба там взять неоткуда.
        node_id = storage.add_node(
            title="Camera: what is wrong?",
            node_type="choice",
            after_node_id="root",
            after_option_id="robot",
            created_by="test",
        )
        self.assertIsNotNone(node_id, "узел не создался")
        self.addCleanup(storage.delete_node, node_id, updated_by="cleanup")

        option_id = storage.add_option(
            node_id, "Camera is dead", next_node="summary", created_by="test"
        )
        self.assertIsNotNone(option_id, "кнопка не создалась")
        self.addCleanup(storage.delete_option, node_id, option_id, updated_by="cleanup")

        return node_id, option_id

    def test_linking_a_template_keeps_the_button_an_addition(self):
        """Привязка заготовки не превращает добавленную кнопку во встроенную."""
        node_id, option_id = self._web_error_button()

        self.assertTrue(
            storage.link_option_to_template(node_id, option_id, 38, updated_by="test"),
            "привязка заготовки не прошла",
        )

        rows = storage.load_option_rows() or []
        row = next(
            (r for r in rows
             if str(r.get("node_id")) == node_id
             and str(r.get("option_id")) == option_id),
            None,
        )

        self.assertIsNotNone(row, "строка кнопки пропала")
        self.assertFalse(
            row.get("is_builtin"),
            "кнопка из веба помечена встроенной — карта покажет у блока 0 кнопок",
        )

    def test_the_button_is_visible_on_the_map_after_linking(self):
        """
        Следствие, которое видел человек: кнопка **остаётся в карте**.

        Проверяется через тот же путь, что и карта (`effective_options` по
        б��зовому дереву), а не через `is_builtin` напрямую: важно именно то, что
        кнопка не пропадает.
        """
        node_id, option_id = self._web_error_button()

        self.assertTrue(
            storage.link_option_to_template(node_id, option_id, 38, updated_by="test")
        )

        shown = storage.effective_options(
            DEFAULT_TREE, node_id, storage.load_option_rows() or []
        )
        ids = {item["id"] for item in shown}

        self.assertIn(
            option_id, ids,
            "кнопка из веба исчезла из карты после привязки заготовки",
        )


class StubFlagMatchesWhetherTheBranchWorks(EditorStorageTestCase):
    """
    🔴 **«Черновая ветка» — только пока у неё НЕТ кнопок.**

    Живой дефект владельца 13.10.2026. Веб показывал на **рабочей** ветке
    `Fingers`:

        This branch is not in use: the bot does not write a report from it.

    А ветка отчёт писала: оператор проходил `Fingers → ошибка → причина → итог`,
    и в журнал ложилась запись. Ложная тревога хуже молчания — по ней человек
    идёт чинить то, что не сломано.

    **Причина.** Признак `is_stub` ставился **при создании** узла из веба
    (`is_stub = тип == CHOICE`) и больше не пересчитывался: кнопки добавляли, а
    признак оставался `True`. Бот это учитывал (`is_stub and not node.options`),
    а веб смотрел **только** `is_stub` — отсюда расхождение «бот пишет отчёт,
    веб говорит, что не пишет».
    """

    def _node_row(self, node_id: str) -> dict:
        return {
            "node_id": node_id,
            "node_type": "choice",
            "title": "Camera: what is wrong?",
            "next_node": None,
            "hidden": False,
            "deleted": False,
        }

    def test_draft_without_buttons_is_flagged(self):
        """Узел без кнопок — черновой, и это правда: приём по нему не идёт."""
        tree = storage.build_tree(
            DEFAULT_TREE, [], [self._node_row("draft_camera")]
        )
        node = tree.node("draft_camera")

        self.assertTrue(node.is_stub, "черновой узел без кнопок не помечен")
        self.assertEqual(node.options, ())

    def test_working_branch_is_not_flagged(self):
        """
        🔴 Узел **с кнопками** — рабочий, и предупреждения быть не должно.

        Именно это было сломано: `is_stub` оставался `True`, и веб показывал
        «ветка не в работе» на ветке, которая пишет отчёт.
        """
        options = [{
            "node_id": "draft_camera", "option_id": "camera_dead",
            "label": "Camera is dead", "next_node": "summary",
            "hidden": False, "deleted": False, "sort_order": 10,
        }]

        tree = storage.build_tree(
            DEFAULT_TREE, options, [self._node_row("draft_camera")]
        )
        node = tree.node("draft_camera")

        self.assertEqual(len(node.options), 1, "кнопка не привязалась")
        self.assertFalse(
            node.is_stub,
            "узел с кнопками помечен черновым — веб покажет ложное "
            "«This branch is not in use»",
        )

    def test_flag_clears_when_the_first_button_is_added(self):
        """
        Признак **пересчитывается** при пересборке узла, а не наследуется.

        Проверка отделена от предыдущей намеренно: там узел собирается сразу с
        кнопкой, а здесь — сначала черновой, потом к нему добавляют кнопку.
        Дефект был именно во втором случае: `replace()` не пересчитывал поле, и
        узел, переживший добавление кнопки, оставался черновым.
        """
        rows = [self._node_row("draft_camera")]

        before = storage.build_tree(DEFAULT_TREE, [], rows).node("draft_camera")
        self.assertTrue(before.is_stub)

        options = [{
            "node_id": "draft_camera", "option_id": "camera_dead",
            "label": "Camera is dead", "next_node": "summary",
            "hidden": False, "deleted": False, "sort_order": 10,
        }]
        after = storage.build_tree(DEFAULT_TREE, options, rows).node("draft_camera")

        self.assertFalse(
            after.is_stub,
            "после добавления кнопки признак не снялся — веб покажет "
            "«ветка не в работе» на рабочей ветке",
        )

    def test_draft_is_flagged_through_the_public_wrapper(self):
        """
        🔴 **Черновой узел помечается, даже когда переданы ВСЕ строки кнопок.**

        `tree_view` (карта веба) зовёт `node_from_rows` и передаёт **всю**
        таблицу кнопок, а не кнопки одного узла. Первая версия исправления
        проверяла `not option_rows` — и на этом пути условие было ложно
        **всегда**: настоящий черновой узел переставал помечаться, и карта
        молчала о том, что приём по ветке не идёт.

        Проверка идёт через **публичную обёртку** намеренно: дефект жил именно
        в ней (в `build_tree` строки фильтруются по `node_id`, там было верно).
        """
        rows = [self._node_row("draft_camera")]
        # Чужие кнопки: они не должны влиять на признак этого узла.
        options = [
            {"node_id": "other_node", "option_id": "o1", "label": "Other",
             "next_node": "summary", "hidden": False, "deleted": False,
             "sort_order": 10},
        ]

        node = storage.node_from_rows("draft_camera", rows[0], options)

        self.assertIsNotNone(node)
        self.assertTrue(
            node.is_stub,
            "черновой узел не помечен, когда переданы кнопки ДРУГИХ узлов",
        )
        self.assertEqual(node.options, (), "чужие кнопки привязались к узлу")

    def test_working_node_is_not_flagged_through_the_public_wrapper(self):
        """Обратная сторона: со **своими** кнопками узел рабочий."""
        rows = [self._node_row("draft_camera")]
        options = [
            {"node_id": "draft_camera", "option_id": "camera_dead",
             "label": "Camera is dead", "next_node": "summary",
             "hidden": False, "deleted": False, "sort_order": 10},
        ]

        node = storage.node_from_rows("draft_camera", rows[0], options)

        self.assertFalse(
            node.is_stub,
            "узел со своими кнопками помечен черновым",
        )

    def test_editor_note_follows_the_same_rule(self):
        """
        Пометка в Telegram-редакторе — по тому же правилу.

        Один `node.is_stub` показывал «draft branch» и там, где кнопки уже есть.
        Человек читал это как «приём не идёт» и шёл чинить рабочую ветку.
        """
        from equipment_intake.editor import _node_caption

        rows = [self._node_row("draft_camera")]

        draft = storage.build_tree(DEFAULT_TREE, [], rows)
        text = _node_caption(draft, draft.node("draft_camera"), "admin")
        self.assertIn("draft branch", text, "черновая ветка не помечена")

        options = [{
            "node_id": "draft_camera", "option_id": "camera_dead",
            "label": "Camera is dead", "next_node": "summary",
            "hidden": False, "deleted": False, "sort_order": 10,
        }]
        working = storage.build_tree(DEFAULT_TREE, options, rows)
        text2 = _node_caption(working, working.node("draft_camera"), "admin")
        self.assertNotIn(
            "draft branch", text2,
            "рабочая ветка помечена как черновая — ложная подсказка",
        )


class WebCreatedBlocksAreEditable(EditorStorageTestCase):
    """
    🔴 Блоки и кнопки, **созданные из веба**, полностью редактируемы.

    Оба дефекта ниже воспроизведены на живом стенде 13.10.2026, и оба — про одно
    и то же: проверка «узел существует» смотрела **только в код**
    (`DEFAULT_TREE`), а блок, созданный из веба, живёт строкой в базе.

    Требование владельца, ради которого это нужно, дословно: «на лету добавлять
    удалять новые блоки с вопросами или кнопками… максимально гибко настраивать
    бота как только я хочу через веб а не только через код».
    """

    def _web_block(self) -> str:
        """Блок, созданный из веба; убирается после теста в любом случае."""
        node_id = storage.add_node(
            title="Web block",
            node_type="choice",
            after_node_id="root",
            after_option_id="robot",
            created_by="web",
        )

        self.assertIsNotNone(node_id, "блок из веба не создался")
        self.addCleanup(storage.delete_node, node_id, updated_by="cleanup")

        return node_id

    def test_web_block_is_absent_from_the_code_tree(self):
        """
        Признак, на котором ломались проверки: в коде блока **нет**.

        Без этой проверки следующие две проходили бы и по другой причине — если
        `add_node` вдруг стал бы править `DEFAULT_TREE`, они бы «работали», а
        проверяли не то.
        """
        node_id = self._web_block()

        self.assertIsNone(DEFAULT_TREE.node(node_id))
        self.assertIn(node_id, storage._known_node_ids(DEFAULT_TREE))

    def test_web_block_can_be_dragged(self):
        """
        🔴 Дефект: блок виден на карте, но перетащить его было нельзя.

        `update_node_position` проверял узел по коду и отвечал `False`, холст
        показывал «unknown node» (HTTP 400), и позиция не сохранялась. В журнале
        бота это выглядело как **пять отказов подряд** после создания блока.
        """
        node_id = self._web_block()

        saved = storage.update_node_position(node_id, 123.0, 45.0, updated_by="web")

        self.assertTrue(saved, "блок из веба не перетаскивается")

        row = next(
            r for r in self.db.rows["telegram_intake_nodes"]
            if r.get("node_id") == node_id
        )

        self.assertEqual(row.get("position_x"), 123.0)
        self.assertEqual(row.get("position_y"), 45.0)

    def test_web_option_can_be_linked_to_a_template(self):
        """
        🔴 Дефект: у кнопки на блоке из веба выбор заготовки молча не сохранялся.

        `link_option_to_template` тоже проверял узел по коду и возвращал `False`.
        В редакторе это выглядело как «выбрал заготовку, а связи нет» — без
        ошибки и без подсказки.
        """
        node_id = self._web_block()
        option_id = storage.add_option(node_id, "Web cause", created_by="web")

        self.assertIsNotNone(option_id, "кнопка на блоке из веба не создалась")

        linked = storage.link_option_to_template(
            node_id, option_id, 53, updated_by="web"
        )

        self.assertTrue(linked, "связь с заготовкой на блоке из веба не сохранилась")

    def test_linking_is_not_weakened_for_unknown_nodes(self):
        """
        Обратная сторона: **несуществующий** узел по-прежнему отвергается.

        Без этой проверки ослабление валидации прошло бы незамеченным: связь
        завелась бы на узел-опечатку, и её не показал бы ни один экран.
        """
        self.assertFalse(
            storage.link_option_to_template("no_such_node", "x", 53),
            "связь завелась на несуществующий узел",
        )
        self.assertFalse(
            storage.update_node_position("no_such_node", 1.0, 2.0),
            "позиция записалась несуществующему узлу",
        )


class CauseTemplateLinksDoNotLeak(EditorStorageTestCase):
    """
    🔴 Связь заготовки не «утекает» между модулями.

    **Дефект, найденный на живой базе 13.10.2026 — 32 утечки.** У причин id
    повторяются: `human_error` и `software_fault` есть у девяти модулей,
    `mechanical_wear` — у восьми. Карта связей держала **голый ключ по
    `option_id`**, поэтому связь, выставленная у одной причины, применялась ко
    всем одноимённым: шасси с причиной «Mechanical wear» получало шаблон
    безопасности 65 («Robot safety device triggered») — не свою ошибку.

    Утечка появилась не сама: пока заготовки висели на кнопках **ошибок**, их id
    были уникальны (11 кнопок, 0 повторов), и голый ключ был безопасен. После
    переноса заготовок на **причины** он стал неоднозначным.

    Голый ключ оставлен только для слоя ошибок — там он по-прежнему однозначен.
    """

    def test_bare_key_is_kept_for_errors_only(self):
        """
        Голый ключ есть у ошибки (совместимость) и **нет** у причины.

        Совместимость обязательна: связи ошибок уже сохранены в базе этой схемой,
        и их потеря «выключила бы» заготовки у владельца.
        """
        storage.link_option_to_template("robot_errors_chassis", "chassis_went_off_road", 120)
        storage.link_option_to_template("ask_cause_safety", "mechanical_wear", 65)
        storage.reset_links_cache()

        links = storage.option_template_links()

        self.assertEqual(links.get("chassis_went_off_road"), 120, "связь ошибки потерялась")
        self.assertEqual(links.get("ask_cause_safety::mechanical_wear"), 65)
        self.assertNotIn(
            "mechanical_wear", links,
            "голый ключ причины снова появился — связь утечёт в другие модули",
        )

    def test_cause_does_not_inherit_another_modules_template(self):
        """
        Причина одного модуля не берёт заготовку одноимённой причины другого.
        """
        from equipment_intake.error_templates import linked_template

        # 🔴 **Заготовка обязана быть в справочнике.** Без неё `resolve` не
        # найдёт строку по `template_id` и вернёт `None` **независимо от
        # утечки** — проверка проходила бы и на сломанном коде. Поймано
        # негативной проверкой: при откате фикса тест оставался зелёным.
        self.db.rows[storage.TEMPLATES_TABLE] = [
            {"id": 65, "employee_title": "Robot safety device triggered"},
        ]

        storage.link_option_to_template("ask_cause_safety", "mechanical_wear", 65)
        storage.reset_links_cache()
        links = storage.option_template_links()

        # Одноимённая причина **другого** модуля: своей связи нет.
        # Без защиты она нашла бы 65 по голому ключу и утекла бы в рог.
        found = linked_template(
            [{"node_id": "robot_errors_horn", "option_id": "horn_cant_extend",
              "label": "Can't extend/expand the HORN"},
             {"node_id": "ask_cause_horn", "option_id": "mechanical_wear",
              "label": "Mechanical wear"}],
            self.db.rows[storage.TEMPLATES_TABLE],
            links,
        )

        self.assertIsNone(found, "причина рога взяла заготовку безопасности")

    def test_cause_uses_its_own_template(self):
        """Обратная сторона: своя связь причины работает."""
        from equipment_intake.error_templates import linked_template

        self.db.rows[storage.TEMPLATES_TABLE] = [
            {"id": 65, "employee_title": "Robot safety device triggered"},
        ]

        storage.link_option_to_template("ask_cause_safety", "sensor_fault", 65)
        storage.reset_links_cache()
        links = storage.option_template_links()

        found = linked_template(
            [{"node_id": "ask_cause_safety", "option_id": "sensor_fault",
              "label": "Sensor fault"}],
            self.db.rows[storage.TEMPLATES_TABLE],
            links,
        )

        self.assertIsNotNone(found, "своя связь причины не сработала")
        self.assertEqual(found["id"], 65)


class EditorCreatesStubChecks(EditorStorageTestCase):
    """`/tree` при добавлении ошибки заводит заготовку в `issue_templates`.

    Требование владельца 12.10.2026: «при добавление новой ошибки делать
    заготовку тоже чтобы потом были поля которые будут подставлены
    автоматически при копирование».

    Проверяется **через `editor._apply_edit`**, а не через `storage` напрямую:
    владельцу важно, что это делает **бот по кнопке**, а не что функция умеет
    создавать строку.
    """

    def setUp(self) -> None:
        super().setUp()
        from equipment_intake import error_templates, editor

        self.editor = editor
        self.error_templates = error_templates
        # Правка узлов и вариантов доступна только админу, но `_apply_edit` роли
        # не проверяет (это делает `handle_callback`), поэтому здесь достаточно
        # отправителя с id.
        self.sender = {"id": 12345}

    def _error_node(self):
        for node_id, node in DEFAULT_TREE.nodes.items():
            if node_id.startswith("robot_errors_") and node.options:
                return node

        self.fail("в дереве нет узла слоя ошибок")

    def test_adding_an_error_creates_and_links_a_stub(self):
        """🔴 Добавление новой ошибки создаёт заготовку **и** связывает её.

        Поля заготовки: название (его ввёл человек) и модуль (следует из узла).
        «Что было сделано» и время решения остаются пустыми — их знает человек.
        """
        node = self._error_node()

        ok, notice, option_id = self.editor._apply_edit(
            self.editor.KIND_ADD_LABEL, node.id, None, "Conveyor jammed", self.sender
        )

        self.assertTrue(ok, notice)
        self.assertIsNotNone(option_id)

        rows = self.db.rows.get(storage.TEMPLATES_TABLE, [])
        self.assertEqual(len(rows), 1, "заготовка не создана")

        row = rows[0]
        self.assertEqual(row["employee_title"], "Conveyor jammed")
        self.assertEqual(row["equipment_type"], "Equipment")
        self.assertIsNone(row["recovery_title"])
        self.assertIsNone(row["solving_time"])

        # Связь с новым вариантом выставлена — иначе подстановки не будет.
        self.assertEqual(
            storage.option_template_id(node.id, option_id), row["id"],
            "заготовка создана, но не связана с ошибкой",
        )
        self.assertIn("template", notice.lower())

    def test_module_comes_from_the_error_node(self):
        """`issue_type` заготовки — модуль из id узла слоя ошибок."""
        node = self._error_node()
        expected = self.error_templates.module_from_node(node.id)

        self.editor._apply_edit(
            self.editor.KIND_ADD_LABEL, node.id, None, "Some new failure", self.sender
        )

        row = self.db.rows[storage.TEMPLATES_TABLE][0]
        self.assertEqual(row["issue_type"], expected)
        self.assertTrue(expected, "модуль не определён из узла ошибок")

    def test_option_is_still_added_when_the_catalogue_is_down(self):
        """Сбой справочника **не отменяет** добавление ошибки.

        Ошибка уже сохранена и работает; связь можно поставить позже. Обратное
        поведение («не добавили, потому что справочник молчит») лишало бы
        оператора кнопки из-за чужого сбоя.

        **Негативная проверка:** убрать `try/except` вокруг создания заготовки —
        тест падает с проброшенным исключением.
        """
        node = self._error_node()

        def boom(*_args, **_kwargs):
            raise RuntimeError("catalogue is down")

        with mock.patch.object(storage, "create_template", side_effect=boom):
            ok, notice, option_id = self.editor._apply_edit(
                self.editor.KIND_ADD_LABEL, node.id, None, "New failure", self.sender
            )

        self.assertTrue(ok, notice)
        self.assertIsNotNone(option_id, "вариант не добавлен из-за сбоя справочника")
        self.assertIn("template was not created", notice)
        # Вариант действительно сохранился.
        self.assertTrue(
            any(r.get("option_id") == option_id
                for r in self.db.rows.get(storage.OPTION_TABLE, []))
        )

    # ---------- правка полей заготовки из карточки варианта ----------

    def _linked(self):
        """Вариант узла ошибок, связанный с заготовкой в тестовой базе."""
        node = self._error_node()
        template_id = storage.create_template("Went off the road", module="Chassis")
        storage.link_option_to_template(node.id, node.options[0].id, template_id)
        storage.reset_links_cache()

        return node, node.options[0], template_id

    def test_template_description_edit_reaches_the_catalogue(self):
        """«Описание ошибки» из карточки уезжает в `issue_description`.

        Именно это поле подставляется в журнал, и требование владельца звучит
        буквально: «на сервере в столбцы с описанием ошибки».
        """
        node, option, template_id = self._linked()

        ok, notice, shown = self.editor._apply_edit(
            self.editor.KIND_TEMPLATE_DESCRIPTION, node.id, option.id,
            "Robot drove off the road", self.sender,
        )

        self.assertTrue(ok, notice)
        self.assertEqual(shown, option.id)
        row = self.db.rows[storage.TEMPLATES_TABLE][0]
        self.assertEqual(row["issue_description"], "Robot drove off the road")
        # Название не тронуто: поля правятся по одному.
        self.assertEqual(row["employee_title"], "Went off the road")
        self.assertIn("journal", notice)

    def test_template_recovery_edit_reaches_the_catalogue(self):
        """«Что было сделано» уезжает в `recovery_title`."""
        node, option, _ = self._linked()

        ok, notice, _ = self.editor._apply_edit(
            self.editor.KIND_TEMPLATE_RECOVERY, node.id, option.id,
            "Move the robot back to the QR code", self.sender,
        )

        self.assertTrue(ok, notice)
        self.assertEqual(
            self.db.rows[storage.TEMPLATES_TABLE][0]["recovery_title"],
            "Move the robot back to the QR code",
        )

    def test_template_solving_time_edit_reaches_the_catalogue(self):
        """Время решения уезжает числом в `solving_time`."""
        node, option, _ = self._linked()

        ok, notice, _ = self.editor._apply_edit(
            self.editor.KIND_TEMPLATE_SOLVING_TIME, node.id, option.id, "6", self.sender
        )

        self.assertTrue(ok, notice)
        self.assertEqual(self.db.rows[storage.TEMPLATES_TABLE][0]["solving_time"], 6)

    def test_template_solving_time_rejects_garbage(self):
        """Нечисловое время решения — отказ, а не молчаливый ноль.

        Ноль означал бы «решается мгновенно», и метрика времени решения
        испортилась бы. Прочерк `-` — осознанная очистка (ставит 0).
        """
        node, option, _ = self._linked()

        ok, notice, _ = self.editor._apply_edit(
            self.editor.KIND_TEMPLATE_SOLVING_TIME, node.id, option.id, "soon", self.sender
        )

        self.assertFalse(ok)
        self.assertIn("number", notice)
        self.assertIsNone(self.db.rows[storage.TEMPLATES_TABLE][0]["solving_time"])

        # А прочерк — очистка, и это уже успех.
        ok, _notice, _ = self.editor._apply_edit(
            self.editor.KIND_TEMPLATE_SOLVING_TIME, node.id, option.id, "-", self.sender
        )

        self.assertTrue(ok)
        self.assertEqual(self.db.rows[storage.TEMPLATES_TABLE][0]["solving_time"], 0)

    def test_template_dash_clears_the_field(self):
        """Дефис очищает поле заготовки: иначе стереть текст из Telegram нельзя."""
        node, option, _ = self._linked()

        self.editor._apply_edit(
            self.editor.KIND_TEMPLATE_RECOVERY, node.id, option.id, "Do something", self.sender
        )
        ok, notice, _ = self.editor._apply_edit(
            self.editor.KIND_TEMPLATE_RECOVERY, node.id, option.id, "-", self.sender
        )

        self.assertTrue(ok, notice)
        self.assertEqual(self.db.rows[storage.TEMPLATES_TABLE][0]["recovery_title"], "")

    def test_template_field_edit_creates_a_stub_for_a_builtin_option(self):
        """Правка полей у варианта **без** связи сначала заводит заготовку.

        Встроенные ошибки (`chassis_went_off_road` в коде) заводились до этой
        правки, и заготовки у них нет. Кнопка «Issue descr.» обязана работать и
        для них — иначе владелец не смог бы заполнить поля у 11 ошибок схемы.
        """
        node = self._error_node()
        option = node.options[0]

        self.assertIsNone(storage.option_template_id(node.id, option.id))

        ok, notice, _ = self.editor._apply_edit(
            self.editor.KIND_TEMPLATE_RECOVERY, node.id, option.id,
            "Restart the module", self.sender,
        )

        self.assertTrue(ok, notice)
        rows = self.db.rows.get(storage.TEMPLATES_TABLE, [])
        self.assertEqual(len(rows), 1, "заготовка не создана для встроенной ошибки")
        self.assertEqual(rows[0]["recovery_title"], "Restart the module")
        self.assertIsNotNone(storage.option_template_id(node.id, option.id))

    def test_template_prompt_shows_the_current_value(self):
        """Подсказка показывает текущее значение — править вслепую нельзя."""
        node, option, _ = self._linked()

        self.editor._apply_edit(
            self.editor.KIND_TEMPLATE_RECOVERY, node.id, option.id,
            "Lift the robot", self.sender,
        )

        current = self.editor._template_field(
            self.editor.KIND_TEMPLATE_RECOVERY, node.id, option.id
        )

        self.assertEqual(current, "Lift the robot")

    def test_missing_link_is_shown_in_the_card(self):
        """Карточка варианта говорит, что связи нет, а не молчит о ней.

        Иначе «связано» и «не связано» выглядят одинаково, и проверить связь
        можно только через базу.
        """
        node = self._error_node()
        item = dict(storage.effective_options(
            DEFAULT_TREE, node.id, storage.load_option_rows() or []
        )[0])

        caption = self.editor._option_caption(
            item, self.editor.ROLE_ADMIN, node_id=node.id
        )

        self.assertIn("Issue template", caption)
        self.assertIn("not linked", caption)

    def test_link_is_shown_in_the_card(self):
        """Со связью карточка показывает её id и название шаблона."""
        node, option, template_id = self._linked()
        item = dict(storage.effective_options(
            DEFAULT_TREE, node.id, storage.load_option_rows() or []
        )[0])

        caption = self.editor._option_caption(
            item, self.editor.ROLE_ADMIN, node_id=node.id
        )

        self.assertIn(f"#{template_id}", caption)
        self.assertIn("Went off the road", caption)
        self.assertIn("what was done: — to fill in —", caption)

    def test_contributor_does_not_see_the_template_section(self):
        """`contributor` связью не управляет — и раздел ему не показывается."""
        node, _option, _tid = self._linked()
        item = dict(storage.effective_options(
            DEFAULT_TREE, node.id, storage.load_option_rows() or []
        )[0])

        caption = self.editor._option_caption(
            item, self.editor.ROLE_CONTRIBUTOR, node_id=node.id
        )

        self.assertNotIn("Issue template", caption)
