"""
Production дерево решений для сбора ошибок оборудования.

Пакет используется production ботом для пошагового сбора ошибок.

Структура:
    types.py        — модель дерева (Node/Option/Step/DecisionTree) + валидация
    tree_config.py  — production дерево и загрузка дерева (конфиг/файл/backend)
    engine.py       — чистая логика переходов, пути, сброса и summary
    keyboards.py    — отрисовка inline-клавиатуры Telegram
    flow.py         — сессии, обработка фото/нажатий/текста, сохранение итога
"""

from .engine import Breadcrumb, EngineError, Session
from .tree_config import DEFAULT_TREE, load_tree
from .types import DecisionTree, Node, NodeType, Option, Step

__all__ = [
    "Breadcrumb",
    "EngineError",
    "Session",
    "DEFAULT_TREE",
    "load_tree",
    "DecisionTree",
    "Node",
    "NodeType",
    "Option",
    "Step",
]
