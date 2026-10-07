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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import equipment_intake.storage as storage  # noqa: E402
from equipment_intake.tree_config import DEFAULT_TREE  # noqa: E402


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


def _install(db: FakeDB) -> None:
    module = types.ModuleType("sendToDataBase")
    module.rest_get = db.rest_get
    module.rest_upsert = db.rest_upsert
    module.rest_delete = db.rest_delete
    def _post(table, payload, **kwargs):
        # add_option пишет через rest_post: заглушка должна реально сохранять,
        # иначе тесты «проходят» на пустой базе и ничего не проверяют.
        db.rows.setdefault(table, []).append(dict(payload))
        return [payload]

    module.rest_post = _post
    sys.modules["sendToDataBase"] = module
    # storage проверяет готовность базы чтением колонки is_builtin
    storage.reset_capability_cache()
    storage._v2_state = True
    storage._v2_checked_at = float("inf")


class EditorStorageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.db = FakeDB()
        _install(self.db)
        self.addCleanup(storage.reset_capability_cache)

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
        """
        for node in DEFAULT_TREE.nodes.values():
            if not node.options or node.id == DEFAULT_TREE.root_id:
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
        """
        probes = {"v2": 0, "v1": 0}
        module = types.ModuleType("sendToDataBase")

        def rest_get(table, params=None, optional=False):
            if table == storage.OPTION_TABLE:
                if "is_builtin" in (params or {}).get("select", ""):
                    probes["v2"] += 1
                else:
                    probes["v1"] += 1
            return None  # ни v2-колонок, ни v1-строк

        module.rest_get = rest_get
        module.rest_upsert = lambda *a, **k: [{}]
        module.rest_delete = lambda *a, **k: False
        module.rest_post = lambda *a, **k: [{}]
        sys.modules["sendToDataBase"] = module
        storage.reset_capability_cache()

        for _ in range(5):
            storage.load_option_rows()

        self.assertEqual(probes["v2"], 1, "колонки v2 проверяются заново на каждый вызов")
        self.assertEqual(probes["v1"], 5, "откат к v1 должен происходить каждый раз")

    def test_missing_nodes_table_is_not_queried_on_every_call(self):
        """Отсутствующая таблица правок узлов тоже запоминается.

        `load_node_rows` при отсутствии таблицы возвращает пустой список — это
        нормально, но сам запрос даёт 404 в логе. На каждое сообщение.
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
        storage.reset_capability_cache()

        for _ in range(5):
            storage.load_node_rows()

        self.assertEqual(queries["nodes"], 1, "отсутствующая таблица запрашивается каждый раз")

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
        storage.reset_capability_cache()

        storage.overlay_supported()

        self.assertTrue(
            any("intake_editor_v2" in m for m in records),
            "исчезло сообщение о том, что нужно применить миграцию",
        )


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
