"""Lightweight event hooks (extension points) for the A2A worker adapter.

Hooks let operators observe and react to adapter events -- logging,
metrics, webhooks, audit trails -- without modifying ``server.py``.

Supported events:

========================= ===================================================
Event                     Payload (keyword arguments)
========================= ===================================================
``on_message_received``   ``envelope`` (parsed task envelope), ``task_id``
``on_task_created``       ``task_id``, ``context_id``
``on_task_state_changed`` ``task_id``, ``state``, ``cached``
``on_task_completed``     ``task_id``, ``ok``, ``details``
``before_dispatch``       ``envelope``
``after_dispatch``        ``envelope``, ``result`` (``RuntimeResult``)
========================= ===================================================

Guarantees:

* A hook that raises never breaks the adapter: exceptions are caught and
  logged, and remaining hooks still run.
* Hooks run sequentially in registration order.
* Handlers may be plain callables or ``async`` callables; awaitable
  results are awaited.

Usage in code::

    from hooks import register

    @register("on_task_completed")
    def audit(task_id, ok, details, **_):
        ...

Operator-provided hooks can also be loaded at startup from the module
named by the ``HOOKS_MODULE`` environment variable (a dotted module name
or a filesystem path to a ``.py`` file). In such a module, simply define
functions named after the events, or call :func:`register` explicitly.
See ``examples/example_hooks.py``.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("a2a_adapter.hooks")

EVENTS: tuple[str, ...] = (
    "on_message_received",
    "on_task_created",
    "on_task_state_changed",
    "on_task_completed",
    "before_dispatch",
    "after_dispatch",
)

Handler = Callable[..., Any]

_registry: dict[str, list[Handler]] = {name: [] for name in EVENTS}


def _validate_event(event: str) -> None:
    if event not in _registry:
        raise ValueError(f"Unknown hook event: {event!r}. Known events: {', '.join(EVENTS)}")


def register(event: str, handler: Handler | None = None):
    """Register ``handler`` for ``event``. Usable as a decorator.

    >>> @register("on_task_created")
    ... def my_hook(task_id, context_id, **_): ...
    """
    _validate_event(event)

    def _add(fn: Handler) -> Handler:
        if fn not in _registry[event]:
            _registry[event].append(fn)
        return fn

    if handler is None:
        return _add
    return _add(handler)


def unregister(event: str, handler: Handler) -> bool:
    """Remove a handler. Returns True if it was registered."""
    _validate_event(event)
    try:
        _registry[event].remove(handler)
        return True
    except ValueError:
        return False


def registered(event: str) -> tuple[Handler, ...]:
    """Return the handlers currently registered for ``event``."""
    _validate_event(event)
    return tuple(_registry[event])


def clear(event: str | None = None) -> None:
    """Remove all handlers for one event, or for every event."""
    if event is None:
        for handlers in _registry.values():
            handlers.clear()
    else:
        _validate_event(event)
        _registry[event].clear()


async def emit(event: str, **payload: Any) -> None:
    """Invoke all handlers for ``event`` sequentially.

    Each handler is called with the payload as keyword arguments.
    Exceptions are logged and swallowed: a broken hook must never affect
    the adapter's main flow. Unknown events are logged and ignored here
    (unlike :func:`register`, emitting is always safe).
    """
    handlers = _registry.get(event)
    if handlers is None:
        logger.warning("emit() called with unknown hook event %r", event)
        return
    for handler in list(handlers):
        try:
            result = handler(**payload)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - hooks must never break the adapter
            logger.exception("Hook %s for event %s failed", getattr(handler, "__name__", handler), event)


def _register_module_functions(module: Any) -> list[str]:
    """Auto-register module-level functions named after hook events."""
    added: list[str] = []
    for name in EVENTS:
        fn = getattr(module, name, None)
        if callable(fn):
            register(name, fn)
            added.append(name)
    return added


def load_module(spec: str):
    """Import a hooks module by dotted name or filesystem path.

    Module-level functions named after hook events are registered
    automatically; the module may also call :func:`register` itself.
    Returns the imported module. Raises on import failure -- callers
    that must not fail should use :func:`load_from_env`.
    """
    if spec.endswith(".py") or os.sep in spec:
        path = Path(spec).expanduser()
        module_name = f"a2a_user_hooks_{path.stem}"
        module_spec = importlib.util.spec_from_file_location(module_name, path)
        if module_spec is None or module_spec.loader is None:
            raise ImportError(f"Cannot load hooks module from {spec!r}")
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[module_name] = module
        module_spec.loader.exec_module(module)
    else:
        module = importlib.import_module(spec)
    added = _register_module_functions(module)
    logger.info("Loaded hooks module %r (auto-registered: %s)", spec, ", ".join(added) or "none")
    return module


def load_from_env(env_var: str = "HOOKS_MODULE") -> bool:
    """Load the hooks module named by ``HOOKS_MODULE``, if set.

    Returns True when a module was loaded. Any failure is logged and
    swallowed so a misconfigured hooks module cannot prevent startup.
    """
    spec = os.getenv(env_var, "").strip()
    if not spec:
        return False
    try:
        load_module(spec)
        return True
    except Exception:  # noqa: BLE001 - startup must survive a bad hooks module
        logger.exception("Failed to load hooks module from %s=%r", env_var, spec)
        return False
