from dataclasses import dataclass


@dataclass(slots=True)
class PortfolioLedger:
    capital: float
    step: float
    lots: int = 0
    entry: float = 0
    realized: float = 0
    fees: float = 0
    funding: float = 0
    trade_cash: float = 0

    @property
    def inventory(self) -> float:
        return self.lots * self.step

    @property
    def cash(self) -> float:
        """Synthetic financing balance in the equivalent linear cash representation."""
        return self.capital + self.trade_cash - self.fees + self.funding

    def fill(self, side: int, lots: int, price: float, fee_bps: float) -> float:
        size = lots * self.step
        change = side * lots
        old = self.lots
        if old * change >= 0:
            self.entry = (abs(old) * self.entry + lots * price) / (abs(old) + lots)
        else:
            self.realized += (
                min(abs(old), lots) * self.step * (1 if old > 0 else -1) * (price - self.entry)
            )
            if lots >= abs(old):
                self.entry = price if lots > abs(old) else 0
        self.lots += change
        self.trade_cash -= side * size * price
        fee = size * price * fee_bps / 10000
        self.fees += fee
        return fee

    def equity(self, mid: float) -> float:
        return self.cash + self.inventory * mid

    def unrealized(self, mid: float) -> float:
        return self.inventory * (mid - self.entry)
