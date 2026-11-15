"""Request-scoped context so the shop engine can tag embed orders without a global flag."""
from contextvars import ContextVar
from typing import Any, Dict, List, Optional, Tuple

_info: ContextVar[Optional[Dict[str, Any]]] = ContextVar("embed_turn_info", default=None)
_actions: ContextVar[Optional[List[Dict[str, Any]]]] = ContextVar("embed_turn_actions", default=None)
_product_ids: ContextVar[Optional[List[str]]] = ContextVar("embed_turn_product_ids", default=None)


def begin_embed_turn(info: Dict[str, Any]) -> Tuple[Any, Any, Any]:
    return _info.set(dict(info)), _actions.set([]), _product_ids.set([])


def embed_turn() -> Optional[Dict[str, Any]]:
    return _info.get()


def record_embed_action(action: Dict[str, Any]) -> None:
    bucket = _actions.get()
    if bucket is not None:
        bucket.append(action)


def record_embed_product_ids(product_ids: List[str]) -> None:
    """Product rows the shop handler listed this turn — used for chat product cards."""
    bucket = _product_ids.get()
    if bucket is None:
        return
    for pid in product_ids or []:
        pid = str(pid or "").strip()
        if pid and pid not in bucket:
            bucket.append(pid)


def peek_embed_product_ids() -> List[str]:
    return list(_product_ids.get() or [])


def end_embed_turn(tokens: Tuple[Any, Any, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    actions = list(_actions.get() or [])
    product_ids = list(_product_ids.get() or [])
    _info.reset(tokens[0])
    _actions.reset(tokens[1])
    _product_ids.reset(tokens[2])
    return actions, product_ids
