"""Big QMT position and asset adapters."""

from ..code_utils import normalize_stock_code
from ..models import AssetSnapshot, PositionSnapshot


def _attr(obj, names, default=None):
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def _float_or_none(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Candidate ThinkTrader field names on the ACCOUNT row of get_trade_detail_data.
# The MiniQMT SDK only documents the normalized name (XtAsset.frozen_cash); the
# big QMT ACCOUNT struct is a different surface and brokers vary, so probe the
# plausible spellings the way cash/total_asset already do.
_FROZEN_CASH_FIELDS = (
    "m_dFrozenCash",
    "m_dFrozen",
    "m_dFrozenBalance",
    "m_dFrozenMargin",
    "frozen_cash",
    "frozen",
)
_MARKET_VALUE_FIELDS = (
    "m_dInstrumentValue",
    "m_dStockValue",
    "m_dMarketValue",
    "market_value",
)

# Printed once per process when the frozen field is not found, listing what the
# row actually carries. Guessing a field name and shipping it unverified is how
# the order-direction bug happened; this makes the real name self-reporting.
_missing_field_reported = set()


def _report_missing_field(label, row, candidates):
    if label in _missing_field_reported:
        return
    _missing_field_reported.add(label)
    try:
        available = sorted(name for name in dir(row) if name.startswith("m_"))
    except Exception:
        available = []
    print(
        "[bigqmt_asset] %s not found (tried %s); ACCOUNT row exposes: %s"
        % (label, ", ".join(candidates), ", ".join(available) or "<none>")
    )


def _full_code(instrument_id, exchange_id):
    code = str(instrument_id or "").strip().upper()
    market = str(exchange_id or "").strip().upper()
    if "." in code:
        return normalize_stock_code(code)
    if market in ("SH", "SZ"):
        return normalize_stock_code("%s.%s" % (code, market))
    return normalize_stock_code(code)


class BigQmtPositionProvider:
    def __init__(self, get_trade_detail_data_func, account_type="STOCK"):
        self.get_trade_detail_data = get_trade_detail_data_func
        self.account_type = account_type

    def _require_query_func(self):
        if self.get_trade_detail_data is None:
            raise RuntimeError("get_trade_detail_data is not available in Big QMT runtime")
        return self.get_trade_detail_data

    def get_positions(self, account_id):
        query = self._require_query_func()
        # QMT's get_trade_detail_data can raise on POSITION queries in some
        # states (e.g. context not bound). Degrade to empty like get_asset does.
        try:
            rows = query(account_id, self.account_type, "POSITION") or []
        except Exception:
            return {}
        positions = {}
        for row in rows:
            code = _full_code(
                _attr(row, ("m_strInstrumentID", "instrument_id", "stock_code")),
                _attr(row, ("m_strExchangeID", "exchange_id", "market")),
            )
            positions[code] = PositionSnapshot(
                stock_code=code,
                volume=int(_attr(row, ("m_nVolume", "volume"), 0) or 0),
                available=int(_attr(row, ("m_nCanUseVolume", "available", "can_use_volume"), 0) or 0),
                cost=float(_attr(row, ("m_dOpenPrice", "m_dCostPrice", "cost"), 0.0) or 0.0),
                stock_name=str(_attr(row, ("m_strInstrumentName", "stock_name"), "") or ""),
                market_value=_float_or_none(_attr(row, ("m_dMarketValue", "m_dInstrumentValue", "market_value"))),
                price=_float_or_none(_attr(row, ("m_dLastPrice", "m_dSettlementPrice", "price", "last_price"))),
                open_price=_float_or_none(_attr(row, ("m_dOpenPrice", "m_dCostPrice", "open_price", "cost"))),
                frozen_volume=int(_attr(row, ("m_nFrozenVolume", "frozen_volume"), 0) or 0),
                on_road_volume=int(_attr(row, ("m_nOnRoadVolume", "on_road_volume"), 0) or 0),
                yesterday_volume=int(_attr(row, ("m_nYesterdayVolume", "yesterday_volume"), 0) or 0),
                direction=int(_attr(row, ("m_nDirection", "direction"), 48) or 48),
            )
        return positions

    def get_asset(self, account_id):
        query = self._require_query_func()
        rows = []
        for detail_type in ("ACCOUNT", "ASSET"):
            try:
                rows = query(account_id, self.account_type, detail_type) or []
                if rows:
                    break
            except Exception:
                rows = []
        if not rows:
            return AssetSnapshot(account_id=account_id, cash=None, total_asset=None)

        row = rows[0]
        cash = _attr(row, ("m_dAvailable", "m_dAvailableCash", "available_cash", "cash"))
        total_asset = _attr(row, ("m_dBalance", "m_dAsset", "total_asset", "asset"))
        frozen_cash = _attr(row, _FROZEN_CASH_FIELDS)
        market_value = _attr(row, _MARKET_VALUE_FIELDS)
        if frozen_cash is None:
            _report_missing_field("frozen_cash", row, _FROZEN_CASH_FIELDS)
        if market_value is None and cash is not None and total_asset is not None:
            # Derive only as a last resort. Without frozen_cash this overstates
            # market value by the frozen amount, so subtract it when known.
            market_value = float(total_asset) - float(cash)
            if frozen_cash is not None:
                market_value -= float(frozen_cash)
        return AssetSnapshot(
            account_id=account_id,
            cash=float(cash) if cash is not None else None,
            total_asset=float(total_asset) if total_asset is not None else None,
            frozen_cash=float(frozen_cash) if frozen_cash is not None else None,
            market_value=float(market_value) if market_value is not None else None,
        )
