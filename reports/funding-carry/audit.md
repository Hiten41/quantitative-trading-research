# Independent audit of the original ETH carry replay

Two fresh replays of the published configuration recovered the original results. The reproduction command checks the original input hashes and the complete numerical JSON, then rebuilds the cash journals and every intermediate hourly equity observation from the fills, archived rates, REST records and separate valuation bars.

| Value | Primary | Confirmation |
| --- | --- | --- |
| net | 32.158 | 87.712 |
| spot_raw | 306.810 | 616.770 |
| perp_raw | -308.515 | -616.775 |
| funding | 37.824 | 93.500 |
| spot_fee | 1.997 | 2.922 |
| perp_fee | 0.998 | 1.462 |
| slip | 0.798 | 1.169 |
| dust | 0.168 | 0.229 |
| end_spot | 5,304.246 | 5,613.034 |
| end_perp | 4,727.912 | 4,474.678 |
| post_buy_cash | 4,154.398 | 3,846.152 |
| post_short_wallet | 4,999.578 | 4,999.423 |
| positive | 276 | 273 |
| negative | 0 | 0 |
| exact | 187 | 273 |
| estimated | 89 | 0 |
| estimated_income | 4.604 | 0 |
| causal_net | 32.159 | 87.712 |
| upper_net | 32.161 | 87.712 |
| no_funding | -5.666 | -5.788 |
| combined_stress | 7.059 | 31.904 |
| hours_verified | 2207 | 2183 |
| buy_gross | 0.5006000 | 0.5006000 |
| buy_fee_eth | 0.0005006 | 0.0005006 |
| dust_eth | 0.0000994 | 0.0000994 |

All money amounts are USDT except the ETH quantities. Both wallets started at 5,000. The gross spot buy paid commission in ETH, retained 0.5 ETH for the hedge, and left sub-lot dust valued at zero. The short sold 0.5 linear ETHUSDT contracts measured in ETH equivalent. Spot sale fees and both futures fees were paid from the respective quote wallets. Every leg crosses recorded five-level depth at its own arrival time and pays an additional two basis points. The independent journal valued all 89 estimated payments at 4.604 USDT in total. Omitting these receipts left about 115.27 USDT combined. Switching from the adverse minute mark to the prior close changed combined PnL by about 0.0014 USDT. The two raw price PnLs already include the quoted spread. Subtracting another spread would duplicate a cost.

For each window the sum of spot and futures reference-price PnL plus signed funding, less both fees, slippage and dust, equals the sum of the terminal cash wallets minus 10,000. Both tradable positions close. The spot dust remains physically possible and is written off. Hourly spot close and previous completed minute futures mark produce the reported intermediate equity. The original 25% collateral-to-notional floor rejects unsupported paths and does not simulate liquidation.

Archive rates are decimal fractions per settlement. A short receives quantity × rate × settlement mark. UTC archive and REST timestamps align to the same eight-hour slots, including millisecond variations. All settlements fall strictly after the short fill and before its cover. The predetermined schedule consumes no funding signal. Both quarters happened to have exclusively positive rates. The dated REST records omit the mark for 89 events. The legacy calculation uses the adverse extremum of the settlement minute. That extremum becomes available after settlement. It affects valuation only. The independent causal alternative uses the last completed mark close. The lower/upper sensitivity uses the published minute range, conditional on that range representing the exchange's settlement mark. It cannot establish a guaranteed bound on an unpublished internal price. The new engine includes the causal estimate in its envelope.

The estimated-income row measures all income attributed to those payments. Subtract it from net for the same-path result excluding them. The causal-net and upper-net rows distinguish the uncertainty in their price from the much larger effect of removing those payments entirely. The combined-stress row doubles fees, raises additional slippage to 10 bp per leg and halves positive receipts. Without funding the two cash journals lose money in aggregate.

[Original execution, funding events and balances](results.json) · [Data provenance](sources.json) · [Expanded experiment](final/research.md)
