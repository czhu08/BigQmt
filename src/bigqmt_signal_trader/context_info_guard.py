# -*- coding: utf-8 -*-
"""Make QMT's per-bar ``ContextInfo`` deepcopy safe.

``_PyContextInfo.resume_context_info`` deepcopies the whole ContextInfo
whenever ``barpos`` changes.  ``__PyContext.__dict__`` holds exactly one
mutable container, the quote subscription table ``subMap``, and our RPC
worker threads add/remove subscriptions on it.  A ``copy.deepcopy`` that
iterates ``subMap`` while a worker mutates it raises
``RuntimeError: dictionary changed size during iteration`` inside
``copy._deepcopy_dict``; QMT then prints the error and stops the strategy.

Replacing ``subMap`` with :class:`ThreadSafeSubMap` removes the race: every
mutation takes a re-entrant lock and ``__deepcopy__`` returns the live table
instead of copying it.  Sharing is the correct semantic here -- the
subscription table is live bookkeeping, not per-bar strategy state that
QMT's snapshot/rollback should duplicate.
"""

import threading

__all__ = ["ThreadSafeSubMap", "install_thread_safe_submap"]


class ThreadSafeSubMap(dict):
    """``ContextInfo.subMap`` replacement that survives concurrent deepcopy."""

    def __init__(self, *args, **kwargs):
        self._lock = threading.RLock()
        dict.__init__(self)
        if args or kwargs:
            dict.update(self, *args, **kwargs)

    def __deepcopy__(self, memo):
        # QMT deepcopies ContextInfo on every barpos change; never iterate the
        # live table there.  The table is shared, not snapshotted.
        return self

    def __setitem__(self, key, value):
        with self._lock:
            dict.__setitem__(self, key, value)

    def __delitem__(self, key):
        with self._lock:
            dict.__delitem__(self, key)

    def __contains__(self, key):
        with self._lock:
            return dict.__contains__(self, key)

    def __len__(self):
        with self._lock:
            return dict.__len__(self)

    def __iter__(self):
        with self._lock:
            return iter(list(dict.keys(self)))

    def get(self, key, default=None):
        with self._lock:
            return dict.get(self, key, default)

    def pop(self, key, *default):
        with self._lock:
            return dict.pop(self, key, *default)

    def popitem(self):
        with self._lock:
            return dict.popitem(self)

    def clear(self):
        with self._lock:
            dict.clear(self)

    def update(self, *args, **kwargs):
        with self._lock:
            dict.update(self, *args, **kwargs)

    def setdefault(self, key, default=None):
        with self._lock:
            return dict.setdefault(self, key, default)

    def keys(self):
        with self._lock:
            return list(dict.keys(self))

    def values(self):
        with self._lock:
            return list(dict.values(self))

    def items(self):
        with self._lock:
            return list(dict.items(self))

    def copy(self):
        with self._lock:
            return dict(self)


def install_thread_safe_submap(context_info, log=None):
    """Swap ``context_info.subMap`` for a deepcopy-safe, lock-protected dict.

    No-op when the object has no ``subMap`` (older or non-wrapper QMT
    contexts) and when the guard is already installed.  Never raises.
    """
    try:
        current = getattr(context_info, "subMap", None)
    except Exception:
        return None
    if current is None or isinstance(current, ThreadSafeSubMap):
        return current
    try:
        replacement = ThreadSafeSubMap(current)
        context_info.subMap = replacement
    except Exception:
        if log is not None:
            try:
                log.warning("install thread-safe ContextInfo.subMap failed",
                            exc_info=True)
            except Exception:
                pass
        return None
    return replacement
