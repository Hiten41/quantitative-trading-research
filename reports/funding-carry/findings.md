# ETH spot–perpetual funding carry

The fixed-size hedge earned **+32.16 USDT** in its first historical window and **+87.71 USDT** in the next. Both positions were closed. On the same 10,000 USDT capital base the non-compounded sum is **+119.87 USDT (1.20%)**. The unchanged confirmation rule uses a fresh 10,000 USDT allocation. Funding accounted for 131.32 USDT across the two windows.

[Interactive hourly PnL](equity.html) · [Fills, payments and cash reconciliation](results.json) · [Source files and hashes](sources.json) · [Fixed research parameters](../../configs/funding_carry.json)

## Trading rule

Buy enough ETH on Binance Spot to own 0.5 ETH after the spot buy commission and short 0.5 ETH of the Binance USD-M ETHUSDT perpetual. Hold the matched quantity until the scheduled exit and close both legs. The rule takes every scheduled funding payment, including negative rates. It has no signal, parameter search or knowledge of future funding. A rise in ETH benefits the spot holding and costs the short roughly the same amount. Changes in the spot–perpetual price difference still affect the pair.

The calendar uses the first two complete calendar quarters after the September development sample, with boundary executions set around noon UTC when public depth records are available. The primary and confirmation dates, costs and stresses were fixed before evaluating either PnL. The preceding passive-quoting attempts remain visible in [the earlier study](../adaptation/findings.md).

- **Primary.** Enter 2023-10-01T12:00:00Z and exit 2024-01-01T11:55:00Z.
- **Confirmation.** Enter 2024-01-01T12:00:00Z and exit 2024-04-01T11:55:00Z.

## Results

| USDT unless stated | Primary | Confirmation |
| --- | ---: | ---: |
| Funding | 37.82 | 93.50 |
| Spot–perpetual basis PnL before extra slippage | -1.70 | -0.00 |
| Trading fees paid | 2.99 | 4.38 |
| Additional slippage charged | 0.80 | 1.17 |
| Residual ETH written off at cost | 0.17 | 0.23 |
| Net cash PnL | 32.16 | 87.71 |
| Sampled maximum drawdown | 4.39 | 3.79 |
| Lowest futures wallet equity | 4,651.66 | 4,165.62 |
| Return on total capital | 0.322% | 0.877% |
| Funding events | 276 | 273 |

Drawdown uses hourly spot closes and perpetual mark closes, plus realized terminal cash. Intrahour basis losses may be larger. Idle cash earns zero. Brokerage balances and capital opportunity costs depend on the account.

## What would erase the profit?

| Net PnL, USDT | Primary | Confirmation |
| --- | ---: | ---: |
| Base assumptions | +32.16 | +87.71 |
| Double fees, 10 bp extra slippage per leg | +25.97 | +78.65 |
| Higher costs and half of positive funding | +7.06 | +31.90 |
| Base costs, zero positive funding | -5.67 | -5.79 |

There were 0 negative funding rates among 549 settlements. This sample therefore provides little evidence about prolonged negative funding. The strategy would pay when the rate turns negative. Two contiguous quarters on one instrument are a limited test of persistence.

## Execution and accounting

- All four trades in each window use actual archived Binance spot or USD-M five-level order books captured by Tardis. Entry and exit use marketable orders. Spot executes 250 ms after the scheduled decision, futures 1,250 ms after it. Each order uses the most recent locally observable book at its arrival. The replay rejects quotes older than five seconds, locked or crossed books, and orders exceeding 10% of displayed size across the five levels. A depth-weighted price includes the spread and any walk through the book. Base runs charge another 2 bp adversely on each order.
- Assumed taker fees are 10 bp on spot and 5 bp on futures. The spot buy fee is deducted in ETH. The purchase rounds up to a 0.0001 ETH lot so that the full 0.5 ETH remains available to hedge and sell. The small residual is written off. The spot sale and futures trades pay fees in USDT. Account-specific discounts, leverage rewards and rebates are excluded.
- The spot and futures wallets start with 5,000 USDT each. No borrowing or collateral transfers occur. Every open-position minute is checked using its highest futures mark and the less favorable within-minute funding balance. The run requires futures equity above 25% of short notional throughout. This is a collateral stress check, not a replica of the venue’s liquidation engine.
- Realized funding rates and times are cross-checked between Binance’s checksummed monthly archives and its REST history. The replay verifies a complete eight-hour calendar and holds the short well clear of each boundary. 460 payments use a reported settlement mark. For the remaining 89, it values a receipt at the archived minute’s lowest mark and a debit at the highest. An exchange settlement mark can differ from its published mark stream. These payments are estimates.
- Candle data only values held positions and bounds missing settlement marks. Executions come from recorded depth. Displayed liquidity can disappear before a real order matches, and actual two-leg timing, fees, venue solvency, capital costs and USDT exposure remain risks.
- Net cash is calculated independently by liquidating both wallets and by adding funding and basis PnL, less commissions, adverse slippage and residual write-off. Across the base and stress runs, the largest absolute reconciliation error was 1.52e-12 USDT.

[Binance funding mechanics](https://www.binance.com/en-AE/support/faq/detail/360033525031) · [Binance public archives](https://github.com/binance/binance-public-data) · [Tardis normalized market data](https://docs.tardis.dev/downloadable-csv-files/data-types)

## Reproduce

From the repository root, use the installed research environment.

```powershell
python -m fundingcarry
python -m pytest -q
```

The command verifies the published source hashes, downloads missing data into the ignored data directory, and rebuilds this report. The machine-readable results include each fill, both fees and settlement cash flows. No exchange account is needed.
