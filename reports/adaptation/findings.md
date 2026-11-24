# Can anchored quotes earn back their execution costs?

The selected maker earned **+21.21 USDT after maker fees in development**. Its funding- and close-adjusted outcomes were **-2.08 USDT** on September 7–11 and **-3.44 USDT** on September 13–17, 2023 UTC. Both independent blocks lost money. The arithmetic sum is -5.52 USDT. Both blocks began flat with 10,000 USDT, so the sum describes two separately initialized replays.

[Interactive marked PnL curves](equity.html) · [Candidate selection](selection.json) · [First holdout details](holdout.json) · [Confirmation details](confirmation/holdout.json)

## What changed

The original strategy constantly moved its quotes with the midpoint. The anchored maker submits passive bids 30 bp below a trailing midpoint average and offers 30 bp above it. Its exponential reference has a one-hour half-life and warms up for one hour after startup or a missed refresh. A buy rests at or below the published best bid, and a sell rests at or above the published best ask. The quote stays within the original 0.5 ETH hard position cap, with up to 0.1 ETH per order.

After a fill, the maker tries to reduce inventory near the reference price. If the midpoint moves more than three band widths from the reference, the maker stops adding exposure against the move and places a passive reducing quote at the touch. It resumes that side after price crosses back through the reference. Cancellations and exits remain exposed to latency and strict trade-through. Small residual positions can persist when the venue minimum notional prevents another order.

## Selection and sample integrity

The first development round tested eight combinations of trend filtering, directional passive quoting, half-life and width. All lost money after fees. Before inspecting either evaluation result, four anchored variants were added on the same September 2–6 development data. The selected parameters maximized marked net PnL less estimated terminal close cost and half of maximum drawdown among candidates with fills on both sides and at least 20 touched orders. Two of the twelve configurations earned positive development PnL. Development returns are selection-biased. [First-round record](first-round.json)

September 7–11 was the first fresh holdout. The second five-day window was designated before reading the first holdout PnL. September 12 failed the existing 120-second continuity check. Its raw trades and bookTicker both show about 1,191 seconds without records beginning near 08:33:48 UTC. The confirmation uses the first subsequent contiguous five days, September 13–17. No model parameter changed between these windows. Both results use real Binance USD-M perpetual L1 and trade archives. [First data manifest](quality.json) · [Confirmation data manifest](confirmation/quality.json)

## Performance

All cash values are USDT. Gross PnL includes terminal marked inventory. Marked net deducts the 1 bp maker fee. The adjusted column adds the historical funding-rate estimate and deducts the estimated cost of a terminal unwind. Flat cash would earn 0 USDT under the same zero-interest assumption.

| Window | Policy | Gross | Maker fees | Marked net | Funding estimate | Close cost | Adjusted net | Fills | Max drawdown |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| First holdout | Fixed spread | -84.18 | 100.03 | -184.21 | -0.06 | 0.31 | -184.58 | 27938 | 184.65 |
| First holdout | Inventory skew | 5.30 | 12.52 | -7.22 | 0.00 | 0.39 | -7.60 | 3288 | 12.04 |
| First holdout | Adaptive without skew | 2.52 | 9.65 | -7.14 | 0.10 | 0.39 | -7.43 | 2528 | 22.85 |
| First holdout | Anchored maker | 2.25 | 4.36 | -2.11 | 0.03 | 0.00 | -2.08 | 1410 | 20.14 |
| Confirmation | Fixed spread | -48.14 | 101.81 | -149.95 | -0.14 | 0.15 | -150.24 | 29966 | 150.87 |
| Confirmation | Inventory skew | 4.16 | 12.33 | -8.16 | -0.02 | 0.12 | -8.31 | 3444 | 12.22 |
| Confirmation | Adaptive without skew | 16.14 | 9.31 | 6.84 | 0.07 | 0.08 | 6.82 | 2660 | 15.92 |
| Confirmation | Anchored maker | 1.28 | 4.77 | -3.49 | 0.05 | 0.00 | -3.44 | 1706 | 13.56 |

Across both windows the selected policy paid 9.13 USDT on 91,325.33 USDT of traded notional. Holding the observed fills and funding estimate fixed, the per-side maker fee that would have produced zero adjusted PnL was 0.396 bp. The run assumed 1.000 bp. This threshold describes the sample and is not an available fee quote.

## Cost and fill sensitivity

| Window | Scenario | Maker fee · bp | Participation | Adjusted net · USDT |
| --- | --- | --- | --- | --- |
| First holdout | reference | 1.0 | 0.1 | -2.08 |
| First holdout | higher_fee | 2 | 0.1 | -6.44 |
| First holdout | lower_participation | 1.0 | 0.02 | -4.03 |
| Confirmation | reference | 1.0 | 0.1 | -3.44 |
| Confirmation | higher_fee | 2 | 0.1 | -8.21 |
| Confirmation | lower_participation | 1.0 | 0.02 | -5.76 |

The fee and participation scenarios rerun the complete policy. Funding cash flow uses the signed position immediately before each settlement. When Binance omits its settlement mark, the calculation uses the last published midpoint strictly before settlement. A terminal close uses the displayed bid for long inventory or the ask for short inventory plus an assumed 5 bp taker fee, conditional on enough displayed size. It is an estimate without additional impact or latency. The main replay uses equal 50 ms entry latency, 80 ms cancellation latency, a 5 s refresh and 15 s lifetime across all policies.

## Day-to-day variation

| Window | UTC day | Marked net change · USDT |
| --- | --- | --- |
| First holdout | 2023-09-07 | -1.86 |
| First holdout | 2023-09-08 | 4.64 |
| First holdout | 2023-09-09 | 0.27 |
| First holdout | 2023-09-10 | 4.33 |
| First holdout | 2023-09-11 | -9.49 |
| Confirmation | 2023-09-13 | -2.74 |
| Confirmation | 2023-09-14 | 2.25 |
| Confirmation | 2023-09-15 | -2.98 |
| Confirmation | 2023-09-16 | 0.12 |
| Confirmation | 2023-09-17 | -0.14 |

The daily series uses marked equity before the separate funding sensitivity and terminal unwind. Fixed-horizon markouts, exact order counts and independent trade-ID audits are in the linked JSON results. Two five-day blocks on one instrument provide limited evidence about live profitability.
