"""建玉と損益の計算。

建玉数量は買いが正、売りが負。平均取得単価は建て増しで加重平均し、一部返済では変えない。
売買の向きが逆転する約定(ドテン)は、返済分で損益を確定させ、残りを新しい建玉として扱う。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from daytrade_bot.core.models import Fill, Side

ZERO = Decimal(0)


@dataclass(slots=True)
class Position:
    symbol: str
    qty: int = 0
    avg_price: Decimal = ZERO

    @property
    def side(self) -> Side | None:
        if self.qty > 0:
            return Side.BUY
        if self.qty < 0:
            return Side.SELL
        return None


@dataclass(frozen=True, slots=True)
class RoundTrip:
    """建ててから建玉が0に戻るまでの1回の取引。"""

    symbol: str
    side: Side
    qty: int
    entry_price: Decimal
    exit_price: Decimal
    opened_at: datetime
    closed_at: datetime
    pnl: Decimal  # 手数料控除後
    fees: Decimal

    @property
    def is_win(self) -> bool:
        return self.pnl > 0


@dataclass(slots=True)
class _OpenTrip:
    side: Side
    opened_at: datetime
    max_qty: int
    entry_notional: Decimal
    entry_qty: int
    exit_notional: Decimal = ZERO
    exit_qty: int = 0
    realized: Decimal = ZERO
    fees: Decimal = ZERO


@dataclass(slots=True)
class Portfolio:
    positions: dict[str, Position] = field(default_factory=dict)
    last_prices: dict[str, Decimal] = field(default_factory=dict)
    realized_pnl: Decimal = ZERO  # 手数料控除前
    fees: Decimal = ZERO
    round_trips: list[RoundTrip] = field(default_factory=list)
    _open_trips: dict[str, _OpenTrip] = field(default_factory=dict)

    def position(self, symbol: str) -> Position:
        pos = self.positions.get(symbol)
        if pos is None:
            pos = Position(symbol)
            self.positions[symbol] = pos
        return pos

    def qty(self, symbol: str) -> int:
        pos = self.positions.get(symbol)
        return pos.qty if pos else 0

    def mark(self, symbol: str, price: Decimal) -> None:
        if price <= 0:
            raise ValueError("価格は正の値である必要があります")
        self.last_prices[symbol] = price

    def apply_fill(self, fill: Fill) -> Decimal:
        """約定を反映し、この約定で確定した損益(手数料控除前)を返す。"""
        pos = self.position(fill.symbol)
        signed = fill.qty * fill.side.sign
        realized = ZERO
        self.fees += fill.fee
        trip = self._open_trips.get(fill.symbol)
        if pos.qty == 0 or (pos.qty > 0) == (signed > 0):
            # 新規または建て増し
            new_qty = pos.qty + signed
            pos.avg_price = (pos.avg_price * abs(pos.qty) + fill.price * fill.qty) / abs(new_qty)
            pos.qty = new_qty
            if trip is None:
                trip = _OpenTrip(
                    side=fill.side,
                    opened_at=fill.time,
                    max_qty=abs(new_qty),
                    entry_notional=fill.price * fill.qty,
                    entry_qty=fill.qty,
                )
                self._open_trips[fill.symbol] = trip
            else:
                trip.entry_notional += fill.price * fill.qty
                trip.entry_qty += fill.qty
                trip.max_qty = max(trip.max_qty, abs(new_qty))
            trip.fees += fill.fee
        else:
            # 返済(一部、全部、またはドテン)
            closing = min(fill.qty, abs(pos.qty))
            direction = 1 if pos.qty > 0 else -1
            realized = (fill.price - pos.avg_price) * closing * direction
            remaining_fill = fill.qty - closing
            closing_fee = fill.fee * closing / fill.qty
            if trip is not None:
                trip.exit_notional += fill.price * closing
                trip.exit_qty += closing
                trip.realized += realized
                trip.fees += closing_fee
            pos.qty += closing * -direction
            if pos.qty == 0:
                pos.avg_price = ZERO
                if trip is not None:
                    self._finish_trip(fill.symbol, trip, fill.time)
            if remaining_fill > 0:
                pos.qty = remaining_fill * fill.side.sign
                pos.avg_price = fill.price
                self._open_trips[fill.symbol] = _OpenTrip(
                    side=fill.side,
                    opened_at=fill.time,
                    max_qty=remaining_fill,
                    entry_notional=fill.price * remaining_fill,
                    entry_qty=remaining_fill,
                    fees=fill.fee - closing_fee,
                )
        self.realized_pnl += realized
        self.last_prices.setdefault(fill.symbol, fill.price)
        return realized

    def _finish_trip(self, symbol: str, trip: _OpenTrip, closed_at: datetime) -> None:
        del self._open_trips[symbol]
        self.round_trips.append(
            RoundTrip(
                symbol=symbol,
                side=trip.side,
                qty=trip.max_qty,
                entry_price=trip.entry_notional / trip.entry_qty,
                exit_price=trip.exit_notional / trip.exit_qty,
                opened_at=trip.opened_at,
                closed_at=closed_at,
                pnl=trip.realized - trip.fees,
                fees=trip.fees,
            )
        )

    def unrealized(self, symbol: str) -> Decimal:
        pos = self.positions.get(symbol)
        if pos is None or pos.qty == 0:
            return ZERO
        last = self.last_prices.get(symbol, pos.avg_price)
        return (last - pos.avg_price) * pos.qty

    def total_unrealized(self) -> Decimal:
        return sum((self.unrealized(s) for s in self.positions), ZERO)

    def net_pnl(self) -> Decimal:
        """確定損益 + 評価損益 - 手数料。"""
        return self.realized_pnl + self.total_unrealized() - self.fees

    def exposure(self, symbol: str) -> Decimal:
        pos = self.positions.get(symbol)
        if pos is None or pos.qty == 0:
            return ZERO
        return abs(pos.qty) * self.last_prices.get(symbol, pos.avg_price)

    def total_exposure(self) -> Decimal:
        return sum((self.exposure(s) for s in self.positions), ZERO)

    def open_symbols(self) -> list[str]:
        return [s for s, p in self.positions.items() if p.qty != 0]

    def set_position(self, symbol: str, qty: int, avg_price: Decimal, when: datetime) -> None:
        """証券会社の建玉で初期化する(本番の再起動時)。"""
        pos = self.position(symbol)
        pos.qty = qty
        pos.avg_price = avg_price if qty != 0 else ZERO
        self._open_trips.pop(symbol, None)
        if qty != 0:
            side = Side.BUY if qty > 0 else Side.SELL
            self._open_trips[symbol] = _OpenTrip(
                side=side,
                opened_at=when,
                max_qty=abs(qty),
                entry_notional=avg_price * abs(qty),
                entry_qty=abs(qty),
            )
            self.last_prices.setdefault(symbol, avg_price)

    def reset_day(self) -> None:
        """日次の損益を0に戻す(建玉はそのまま)。"""
        self.realized_pnl = ZERO
        self.fees = ZERO
        self.round_trips.clear()
