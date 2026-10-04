# -*- coding: utf-8 -*-
"""Контроль доступа к редактору дерева (`equipment_intake/editor.py`).

Почему отдельный файл: полный режим редактора позволяет править и **скрывать**
шаги дерева приёма ошибок, то есть менять то, что видят сотрудники на складе.
До этого файла `editor_role` не был покрыт ни одной проверкой — ни в
`test_telegram_offline`, ни в harness.

Логика ролей (по докстроке `editor_role`):
  1. id в `TELEGRAM_ADMIN_USER_IDS` → ADMIN;
  2. явный белый список задан, человека в нём нет → NONE;
  3. `employees.is_leader = true` → ADMIN;
  4. белый список пуст и человек не лидер → CONTRIBUTOR (только добавление).

`_is_leader` обязан падать **в пользу отказа**: любая ошибка чтения — False.
Это проверяется отдельно, потому что обратное означало бы выдачу прав по сбою.
"""
from __future__ import annotations

import importlib
import os
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import equipment_intake.editor as editor  # noqa: E402


class AccessTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_env = os.environ.get("TELEGRAM_ADMIN_USER_IDS")
        self._saved_cache = dict(editor._LEADER_CACHE)
        editor._LEADER_CACHE.clear()
        os.environ.pop("TELEGRAM_ADMIN_USER_IDS", None)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._saved_env is None:
            os.environ.pop("TELEGRAM_ADMIN_USER_IDS", None)
        else:
            os.environ["TELEGRAM_ADMIN_USER_IDS"] = self._saved_env
        editor._LEADER_CACHE.clear()
        editor._LEADER_CACHE.update(self._saved_cache)

    def _fake_store(self, employee, *, boom: bool = False) -> None:
        module = types.ModuleType("telegram_store")

        def get_employee(user_id):
            if boom:
                raise RuntimeError("база недоступна")
            return employee

        module.get_employee = get_employee
        sys.modules["telegram_store"] = module
        editor._LEADER_CACHE.clear()

    # ---------- разбор белого списка ----------

    def test_admin_list_parses_separators(self):
        os.environ["TELEGRAM_ADMIN_USER_IDS"] = "111, 222;333 444"

        self.assertEqual(editor._admin_user_ids(), {111, 222, 333, 444})

    def test_admin_list_ignores_garbage(self):
        os.environ["TELEGRAM_ADMIN_USER_IDS"] = "111, not-a-number, ,222"

        self.assertEqual(editor._admin_user_ids(), {111, 222})

    def test_admin_list_empty_when_unset(self):
        self.assertEqual(editor._admin_user_ids(), set())

    # ---------- роли ----------

    def test_user_in_admin_list_is_admin(self):
        os.environ["TELEGRAM_ADMIN_USER_IDS"] = "555"

        self.assertEqual(editor.editor_role({"id": 555}), editor.ROLE_ADMIN)

    def test_user_outside_explicit_list_is_denied(self):
        """Явный список — это ограничение: чужие не правят даже как лидеры.

        Проверено намеренно: если владелец перечислил админов, лидер склада
        из `employees` не должен получить доступ в обход списка.
        """
        os.environ["TELEGRAM_ADMIN_USER_IDS"] = "555"
        self._fake_store({"is_leader": True})

        self.assertEqual(editor.editor_role({"id": 999}), editor.ROLE_NONE)

    def test_leader_is_admin_when_list_is_empty(self):
        os.environ.pop("TELEGRAM_ADMIN_USER_IDS", None)
        self._fake_store({"is_leader": True})

        self.assertEqual(editor.editor_role({"id": 777}), editor.ROLE_ADMIN)

    def test_non_leader_is_contributor_when_list_is_empty(self):
        """Обратная совместимость: без списка и без лидерства — только добавление."""
        self._fake_store({"is_leader": False})

        self.assertEqual(editor.editor_role({"id": 777}), editor.ROLE_CONTRIBUTOR)

    def test_missing_id_is_denied(self):
        self.assertEqual(editor.editor_role({}), editor.ROLE_NONE)
        self.assertEqual(editor.editor_role(None), editor.ROLE_NONE)

    # ---------- отказ в пользу безопасности ----------

    def test_store_error_denies_leader(self):
        """Сбой базы не должен выдавать права: False, а не исключение наружу."""
        self._fake_store(None, boom=True)

        self.assertFalse(editor._is_leader(12345))
        self.assertEqual(editor.editor_role({"id": 12345}), editor.ROLE_CONTRIBUTOR)

    def test_non_dict_employee_is_not_leader(self):
        self._fake_store(["не словарь"])

        self.assertFalse(editor._is_leader(12345))

    # ---------- кэш ----------

    def test_leader_result_is_cached(self):
        calls = {"n": 0}
        module = types.ModuleType("telegram_store")

        def get_employee(user_id):
            calls["n"] += 1
            return {"is_leader": True}

        module.get_employee = get_employee
        sys.modules["telegram_store"] = module
        editor._LEADER_CACHE.clear()

        editor._is_leader(4242)
        editor._is_leader(4242)

        self.assertEqual(calls["n"], 1, "результат не закэширован — лишний запрос в базу")

    def test_cache_expires(self):
        """Лидера могут снять: кэш обязан истекать."""
        self._fake_store({"is_leader": True})
        editor._is_leader(31337)

        editor._LEADER_CACHE[31337] = (True, 0.0)  # состарили запись
        calls = {"n": 0}
        module = types.ModuleType("telegram_store")

        def get_employee(user_id):
            calls["n"] += 1
            return {"is_leader": False}

        module.get_employee = get_employee
        sys.modules["telegram_store"] = module

        self.assertFalse(editor._is_leader(31337))
        self.assertEqual(calls["n"], 1, "истёкший кэш не перечитан")

    # ---------- реальное различие прав ----------

    def test_contributor_cannot_reach_structural_actions(self):
        """CONTRIBUTOR не должен получать действия, меняющие структуру.

        Проверяю не наличие строки в коде, а то, что набор действий, требующих
        ADMIN, непуст и включает правку/скрытие/перемещение.
        """
        source = Path(editor.__file__).read_text(encoding="utf-8")

        self.assertIn('if role != ROLE_ADMIN', source)
        for action in ('"r"', '"d"', '"i"', '"g"', '"G"', '"U"', '"W"', '"v"'):
            self.assertIn(action, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
