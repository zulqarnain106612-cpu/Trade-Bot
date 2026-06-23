# Triple Barrier Labeling + CPCV
**Domain**: quant_finance | **Tags**: triple, barrier, label, cpcv, cross, validation, purged, combinatorial, afml

## Triple Barrier + CPCV — Architecture Reference

### Triple Barrier (AFML Ch.3)
Labels: +1 (upper hit first), -1 (lower hit first), 0 (time barrier)
Parameters: profit_take=2×ATR, stop_loss=1×ATR, time_barrier=20 bars
Prevents: look-ahead bias via event-driven labeling

### CPCV — Combinatorial Purged Cross-Validation (AFML Ch.7)
Solves: standard k-fold leaks future data into training for financial series
Mechanism:
  1. Split into N groups
  2. Purge: remove samples whose labels overlap with test period
  3. Embargo: remove k bars after each test period
  4. Combinatorial: test all C(N,k) combinations — unbiased Sharpe estimate

### Why this matters for this project
Without CPCV: reported Sharpe inflated by 0.5-2× due to serial correlation
With CPCV: OOS Sharpe is the real number — live gate Sharpe>1.5 is meaningful

### Common mistakes
- Forgetting embargo period: leaks momentum into adjacent bars
- Using accuracy not F1: class imbalance (more 0 labels than ±1) inflates accuracy
- Not checking for concurrent labels: overlapping events need deduplication
