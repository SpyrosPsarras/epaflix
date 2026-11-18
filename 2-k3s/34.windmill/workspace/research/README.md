# Strategy research

Offline research code for the pullback and breakout strategies in `f/screener/strategy_screen.py`.
Nothing here is a Windmill script: `wmill.yaml` syncs only `f/**`, so this folder is never
deployed, and the production screeners are not changed. Tests live in `tests/test_strategy_research.py`.

## What it does

`strategy_research.py` runs a walk-forward parameter study:

1. **Candidates.** Every ticker is scanned once with the loosest value of each varied entry
   threshold. Each candidate keeps its signal-day feature values (bars up to t only). With the
   baseline values, the candidates are exactly the signals of `strategy_screen` (tested).
2. **Ranks.** 6-1 month momentum and volatility-adjusted momentum are ranked per date across all
   liquid tickers on that date.
3. **Simulation.** For one exit configuration at a time (stop buffer, exit rule, holding period),
   every candidate is simulated with the chronology of `strategy_screen.simulate`: the open first,
   then the range; a gap through the stop or target exits at the open; inside a bar the stop wins.
   A trailing stop moves at the close and applies from the next bar.
4. **Configurations.** Entry thresholds and filters are masks over the candidates. One trade per
   ticker at a time. Every configuration is evaluated and written to `configs.csv`.
5. **Selection** uses the development period only and a rule fixed before the run: the median net
   expectancy at 25 bp over the configuration and its numeric neighbours (thin neighbours count as
   0 R), at least 200 pullback / 100 breakout trades per 8 years, and at least 60% positive years.
   Validation and holdout results are reported but never used to choose.
6. **Walk-forward.** Each year, the same rule picks a configuration from all earlier years only,
   and that pick is traded in the year. The concatenated result estimates the whole procedure,
   including its selection bias.
7. **Benchmarks** for the baseline and the shortlist:
   - Matched random entries: the same signal days, a random ticker from the liquid uptrend pool
     (close above SMA200), next-open entry, the trade's stop distance in ATR, its reward in R, and
     the same trail and horizon.
   - QQQ from the same open to the same close.

## Parameter grid (and why)

| Dimension | Values | Rationale |
|---|---|---|
| Pullback touch | 0.1 / 0.25 / 0.5 ATR from EMA20/50 | How strictly "at the moving average" is defined |
| Pullback depth | 0.5 / 1.0 / 1.5 ATR below the 20-day high | Whether a real pullback happened |
| Breakout lookback | 20 / 50 / 100 days | Short, medium and long bases |
| Breakout base rules | none / baseline (age, touches, depth) | Whether the base structure adds anything |
| Breakout volume | 1.0 / 1.5 / 2.0 x the 50-day average | Strength of volume confirmation |
| Stop buffer | 0.2 / 0.5 / 1.0 ATR (pullback), 0.5 / 1.0 ATR (breakout) | Noise tolerance vs risk size |
| Exit | structural target / 2R / 3R / 3 ATR chandelier trail | Fixed targets vs letting winners run |
| Holding period | 10 / 20 / 40 sessions | Swing horizons |
| Regime | all / QQQ above a rising SMA200 / same plus at least 6 of 9 sector ETFs above their SMA200 | Trend and breadth |
| Strength | none / 63-day RS vs QQQ > 0 / RS vs own sector ETF > 0 / top 30% 6-1 momentum / top 30% vol-adjusted momentum | Relative strength and momentum |

Fixed in every configuration: liquidity (price at least $5, $10M average dollar volume as of the
signal day), the baseline trend and reversal-bar rules, maximum risk 2.5 ATR, breakout extension at
most 1 ATR, and structural targets with the baseline minimum reward/risk.

## Data and its biases

- **Prices.** Daily bars from the Finviz chart endpoint (split adjusted; dividend adjustment not
  verified), about 17 years. Universe: today's Finviz list of US stocks with mid cap or larger,
  price above $5 and average volume above 500k.
- **Survivorship.** Delisted and acquired stocks are missing (Finviz returns nothing for them).
  Long results are biased upward. The matched random benchmark is drawn from the same biased pool,
  so the comparison with it removes much of the shared drift, but not all of it.
- **Selection.** Sectors are today's classifications. Liquidity filters use only data up to the
  signal day.
- **Regime and breadth** come from ETFs (QQQ, the 9 original sector SPDRs), which do not suffer
  from survivorship.
- **Overlap.** Trades in different tickers overlap. Drawdown sums R in exit order; it is not a
  portfolio simulation with a position limit.
- **Costs.** Round trip in basis points of the fill price: 0, 10, 25, 50. Selection uses 25 bp.
  There is no market-impact model.
- **Multiple testing.** About 11,000 configurations are tried. Every one is logged. Treat the best
  development results as optimistic: the walk-forward and the validation/holdout periods are the
  evidence that matters.
- **The holdout is not clean.** Part of it (2023-10 onward) was already looked at in the earlier
  baseline diagnostics on 40 and 150 tickers.

## Running

```sh
python research/strategy_research.py --cache /path/to/json_cache --meta meta.json --out results/DATE
```

`--cache` holds one JSON file per ticker (`ticker`, `dates`, `open`, `high`, `low`, `close`,
`volume`). The fetch script is not part of this folder, because it is a one-off download from the
same endpoint `strategy_screen.fetch_bars` uses. `--meta` maps tickers to `{"sector": ...}`.

Results of each run go to `results/<date>/`:
- `configs.csv`: every configuration, with development and validation statistics.
- `report.json` and `report.md`: shortlist, holdout, benchmarks, walk-forward and parameter
  marginals.

`EXPERIMENTS.md` keeps the running log of what was tried and what was rejected.
