# -*- coding: utf-8 -*-
"""QMT's per-bar ContextInfo deepcopy must never iterate our live subMap."""

import copy
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bigqmt_signal_trader.context_info_guard import (  # noqa: E402
    ThreadSafeSubMap,
    install_thread_safe_submap,
)


class _FakePyContext:
    """Mirror of ``_PyContextInfo.__PyContext`` and its ``__deepcopy__``."""

    def __init__(self):
        self.context = None
        self.z8sglma_last_version = None
        self.z8sglma_last_barpos = -1
        self.subMap = ThreadSafeSubMap()

    def __deepcopy__(self, memo):
        new_obj = type(self)()
        for key, value in list(self.__dict__.items()):
            if key == "context":
                setattr(new_obj, key, value)
            elif key == "z8sglma_last_version":
                continue
            else:
                setattr(new_obj, key, copy.deepcopy(value, memo))
        return new_obj


def test_deepcopy_returns_the_live_table():
    table = ThreadSafeSubMap({1: {"func": "subscribe_quote"}})
    assert copy.deepcopy(table) is table


def test_install_swaps_table_and_keeps_entries():
    class Ctx(object):
        pass

    ctx = Ctx()
    ctx.subMap = {3: {"func": "subscribe_whole_quote"}}
    installed = install_thread_safe_submap(ctx)
    assert isinstance(ctx.subMap, ThreadSafeSubMap)
    assert installed is ctx.subMap
    assert dict(ctx.subMap) == {3: {"func": "subscribe_whole_quote"}}
    assert install_thread_safe_submap(ctx) is installed


def test_install_is_a_noop_without_submap():
    class Ctx(object):
        pass

    assert install_thread_safe_submap(Ctx()) is None


def test_qmt_style_snapshot_shares_the_table():
    ctx = _FakePyContext()
    ctx.subMap[9] = {"func": "subscribe_whole_quote"}
    snapshot = copy.deepcopy(ctx)
    assert snapshot.subMap is ctx.subMap


def test_deepcopy_during_concurrent_mutation_does_not_raise():
    ctx = _FakePyContext()
    for index in range(2000):
        ctx.subMap[index] = {"func": "subscribe_quote"}
    stop = threading.Event()

    def mutate():
        key = 10000
        while not stop.is_set():
            ctx.subMap[key] = {"func": "subscribe_whole_quote"}
            ctx.subMap.pop(key, None)
            key += 1

    worker = threading.Thread(target=mutate)
    worker.start()
    try:
        for _ in range(50):
            snapshot = copy.deepcopy(ctx)
            assert snapshot.subMap is ctx.subMap
    finally:
        stop.set()
        worker.join(5)
