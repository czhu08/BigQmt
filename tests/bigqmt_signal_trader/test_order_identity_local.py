# coding: utf-8
"""No-Redis deployments must still attribute strategy_name (issue #156 / #133).

QMT's ORDER/DEAL rows never carry the strategy name -- the terminal filters
by it but does not report it. The bridge answers that with an identity store
keyed by remark, but the store is Redis, so a zmq single-file deployment (no
Redis anywhere) read strategy_name as "" forever.

The handlers now keep an in-process journal as well: submit writes
(account, remark) -> strategy_name, query reads it back for rows QMT could
not name. Redis remains the primary store (survives restarts, covers other
processes); the local journal is the no-Redis floor.
"""

import os
import sys
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.models import OrderSnapshot
from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers

from test_redis_rpc import (  # noqa: E402  -- reuse the established fakes
    FakeMarketData,
    FakePositionProvider,
)
from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway


class _QueryGateway(DryRunOrderGateway):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def query_orders(self, account_id, strategy_name):
        return list(self.rows)


def _row(user_order_id="", strategy_name="", order_sys_id="sys-1"):
    return OrderSnapshot(
        order_sys_id=order_sys_id,
        user_order_id=user_order_id,
        stock_code="601398.SH",
        action="BUY",
        volume=100,
        traded_volume=0,
        status="50",
        strategy_name=strategy_name,
    )


def _handlers(gateway):
    return BigQmtRpcHandlers(
        account_id="acct",
        market_data=FakeMarketData(),
        position_provider=FakePositionProvider(),
        order_gateway=gateway,
        allow_order_methods=True,
    )


def _submit(handlers, remark, strategy_name):
    handlers._handle_submit_order({
        "stock_code": "601398.SH", "action": "BUY", "volume": 100,
        "price": 8.0, "remark": remark, "strategy_name": strategy_name,
        "wait_settlement": False,
    })


class LocalIdentityJournalTest(unittest.TestCase):
    def test_submitted_order_gets_strategy_name_back_without_redis(self):
        gateway = _QueryGateway([_row(user_order_id="sig-1")])
        handlers = _handlers(gateway)
        # No identity Redis anywhere -- the zmq single-file shape.
        self.assertIsNone(handlers._identity_redis())

        _submit(handlers, "sig-1", "my_strat")
        rows = handlers._handle_query_orders({})

        self.assertEqual(rows[0].strategy_name, "my_strat")

    def test_order_not_submitted_here_stays_unnamed(self):
        gateway = _QueryGateway([_row(user_order_id="manual-order")])
        handlers = _handlers(gateway)

        rows = handlers._handle_query_orders({})

        self.assertEqual(rows[0].strategy_name, "")

    def test_journal_is_scoped_by_account(self):
        gateway = _QueryGateway([_row(user_order_id="sig-1")])
        handlers = _handlers(gateway)
        _submit(handlers, "sig-1", "my_strat")

        rows = handlers._handle_query_orders({"account_id": "someone-else"})

        self.assertEqual(rows[0].strategy_name, "")

    def test_expired_entries_do_not_attribute(self):
        gateway = _QueryGateway([_row(user_order_id="sig-1")])
        handlers = _handlers(gateway)
        _submit(handlers, "sig-1", "my_strat")
        key = ("acct", "sig-1")
        ts, name = handlers._order_identity_local[key]
        handlers._order_identity_local[key] = (ts - 90000, name)  # > 24h ago

        rows = handlers._handle_query_orders({})

        self.assertEqual(rows[0].strategy_name, "")

    def test_journal_is_bounded(self):
        handlers = _handlers(_QueryGateway([]))
        for i in range(handlers._ORDER_IDENTITY_LOCAL_LIMIT + 50):
            handlers._remember_order_identity_local("acct", "r%d" % i, "s")

        self.assertEqual(
            len(handlers._order_identity_local),
            handlers._ORDER_IDENTITY_LOCAL_LIMIT)
        self.assertNotIn(("acct", "r0"), handlers._order_identity_local)
        self.assertIn(("acct", "r%d" % (handlers._ORDER_IDENTITY_LOCAL_LIMIT + 49)),
                      handlers._order_identity_local)

    def test_existing_redis_answer_is_not_clobbered_by_local(self):
        gateway = _QueryGateway([_row(user_order_id="sig-1", strategy_name="from_redis")])
        handlers = _handlers(gateway)
        _submit(handlers, "sig-1", "from_local")

        rows = handlers._handle_query_orders({})

        # Row already named (by Redis/terminal): local journal must not overwrite.
        self.assertEqual(rows[0].strategy_name, "from_redis")


if __name__ == "__main__":
    unittest.main()
