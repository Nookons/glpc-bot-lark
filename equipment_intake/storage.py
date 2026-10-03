"""Supabase-backed shared option extensions for the intake decision tree."""
from __future__ import annotations

from dataclasses import replace
from typing import Dict, List, Optional
from .tree_config import DEFAULT_TREE
from .types import DecisionTree, Node, NodeType, Option

OPTION_TABLE = "telegram_intake_options"
_OPTION_NODE_TYPES = (NodeType.CHOICE, NodeType.MULTI, NodeType.YESNO)
MAX_LABEL_LENGTH = 60


def options_for(node_id: str) -> List[dict]:
    from sendToDataBase import rest_get
    rows = rest_get(OPTION_TABLE, {"select": "node_id,option_id,label,next_node,description,icon,only_for", "node_id": f"eq.{node_id}", "order": "created_at.asc"})
    if rows is None:
        return []
    return [{"id": r["option_id"], "label": r["label"], "next_node": r.get("next_node"), "description": r.get("description") or "", "icon": r.get("icon") or "", "only_for": r.get("only_for") or []} for r in rows if isinstance(r, dict)]


def add_option(node_id: str, label: str, description: str = "", icon: str = "", only_for=(), next_node: Optional[str] = None, created_by=None) -> Optional[str]:
    node_id = str(node_id or "").strip()
    node = DEFAULT_TREE.nodes.get(node_id)
    label = str(label or "").strip()
    if not node or node.type not in _OPTION_NODE_TYPES or not label or len(label) > MAX_LABEL_LENGTH:
        return None
    from collections import Counter
    targets = Counter(o.next_node for o in node.options if o.next_node)
    next_node = next_node or (targets.most_common(1)[0][0] if targets else node.next_node)
    if next_node and next_node not in DEFAULT_TREE.nodes:
        return None
    import re
    base = re.sub(r"[^a-z0-9]+", "_", label.casefold()).strip("_")[:24] or "option"
    from sendToDataBase import rest_get, rest_post
    existing = rest_get(OPTION_TABLE, {"select": "option_id", "node_id": f"eq.{node_id}"})
    if existing is None:
        return None
    taken = {o.id for o in node.options} | {str(r.get("option_id")) for r in existing if isinstance(r, dict)}
    option_id = base
    n = 2
    while option_id in taken:
        option_id = f"{base[:20]}_{n}"; n += 1
    row = {"node_id": node_id, "option_id": option_id, "label": label, "next_node": next_node, "description": str(description or "")[:500], "icon": str(icon or "")[:10], "only_for": list(only_for) if not isinstance(only_for, str) else [only_for], "created_by": str(created_by) if created_by is not None else None}
    return option_id if rest_post(OPTION_TABLE, row) is not None else None


def apply_overlay(base: DecisionTree) -> DecisionTree:
    """Read shared additions once, then merge into the immutable base tree."""
    from sendToDataBase import rest_get
    rows = rest_get(OPTION_TABLE, {"select": "node_id,option_id,label,next_node,description,icon,only_for", "order": "created_at.asc"})
    if rows is None:
        return base
    grouped = {}
    for row in rows:
        if isinstance(row, dict):
            grouped.setdefault(str(row.get("node_id") or ""), []).append(row)
    nodes: Dict[str, Node] = dict(base.nodes)
    for node_id, node in base.nodes.items():
        extras = []
        seen = {o.id for o in node.options}
        for row in grouped.get(node_id, []):
            oid, label = str(row.get("option_id") or ""), str(row.get("label") or "")
            target = row.get("next_node")
            if not oid or not label or len(label) > MAX_LABEL_LENGTH or oid in seen or (target and target not in base.nodes):
                continue
            extras.append(Option(id=oid, label=label, next_node=target, description=row.get("description") or "", icon=row.get("icon") or "", only_for=tuple(row.get("only_for") or ())))
            seen.add(oid)
        if extras:
            nodes[node_id] = replace(node, options=node.options + tuple(extras))
    return DecisionTree(root_id=base.root_id, nodes=nodes)
