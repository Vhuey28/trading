"""
Max-profit simulation using existing backtest trades as baseline.
Applies:
- new filters (ADX>25, remove QQQ breakout, etc.)
- new position sizing (fixed large, % equity, ATR risk)
- compounding
Produces comparison report and scaled equity curves.
"""
import csv
from pathlib import Path
from collections import Counter, defaultdict
import json

TRADES_PATH = Path("logs/trades.csv")
OUT_DIR = Path("logs/max_profit_simulation")
OUT_DIR.mkdir(parents=True, exist_ok=True)

rows = list(csv.DictReader(open(TRADES_PATH)))
def pnl(rs): return sum(float(r['pnl']) for r in rs)
def win_rate(rs):
    if not rs: return 0
    return sum(1 for r in rs if float(r['pnl'])>0)/len(rs)*100

# Baseline
baseline_pnl = pnl(rows)
baseline_wr = win_rate(rows)
print(f"Baseline: {len(rows)} trades pnl ${baseline_pnl:.4f} win {baseline_wr:.1f}% at $25")
for notion in [25,1000,5000,10000,20000]:
    factor=notion/25
    print(f"  at ${notion}: pnl ${baseline_pnl*factor:.2f}")

# Filters
# 1) ADX>25
adx25 = [r for r in rows if float(r['adx_at_entry'])>25]
print(f"\nADX>25: {len(adx25)} trades pnl {pnl(adx25):.4f} win {win_rate(adx25):.1f}%")
for n in [25,5000,10000,20000]:
    print(f"  at ${n}: ${pnl(adx25)*n/25:.2f}")

# 2) Remove QQQ breakout (worst single segment)
no_qqq_breakout = [r for r in rows if not (r['symbol']=='QQQ' and r['entry_type']=='breakout entry')]
print(f"\nRemove QQQ breakout: {len(no_qqq_breakout)} trades pnl {pnl(no_qqq_breakout):.4f} win {win_rate(no_qqq_breakout):.1f}%")
for n in [25,5000,10000,20000]:
    print(f"  at ${n}: ${pnl(no_qqq_breakout)*n/25:.2f}")

# 3) Combined max-profit filter: ADX>20 + remove QQQ breakout? Let's test ADX>25 OR pullback
max_profit_filter = [r for r in rows if float(r['adx_at_entry'])>25 or r['entry_type']=='pullback to fast SMA entry']
# Better: keep only trades that would pass new bot filters (ADX>25, volume>0.8, not lunch etc.)
# Approx: ADX>25 and (breakout with confirmed + pullback) => we have ADX>25 already
# For simulation, combine ADX>25 with removing low-quality QQQ breakout where ADX low
filtered = [r for r in rows if float(r['adx_at_entry'])>25]  # strictest profitable
# Keep pullbacks even if ADX lower? pullbacks have higher win anyway
filtered_pullback_keep = [r for r in rows if float(r['adx_at_entry'])>25 or r['entry_type']=='pullback to fast SMA entry']
print(f"\nADX>25 OR pullback: {len(filtered_pullback_keep)} pnl {pnl(filtered_pullback_keep):.4f} win {win_rate(filtered_pullback_keep):.1f}%")
for n in [25,5000,10000,20000]:
    print(f"  at ${n}: ${pnl(filtered_pullback_keep)*n/25:.2f}")

# 4) SPY only (best symbol)
spy_only = [r for r in rows if r['symbol']=='SPY']
print(f"\nSPY only: {len(spy_only)} pnl {pnl(spy_only):.4f} win {win_rate(spy_only):.1f}%")
for n in [25,5000,10000,20000]:
    print(f"  at ${n}: ${pnl(spy_only)*n/25:.2f}")

# Simulate percent equity 20% with compounding starting $50k
def simulate_percent_trades(trades, start_equity=50000, pct=0.20, max_notional=20000, compounding=True):
    equity = start_equity
    total_pnl = 0
    equity_curve = []
    max_dd = 0
    peak = equity
    for r in trades:
        # Use actual entry price to compute qty, but we have pnl already at 25 notional, need to scale
        # Scale factor = notional / 25
        # For percent mode, notional = equity * pct capped at max
        notional = equity * pct if compounding else start_equity * pct
        notional = min(notional, max_notional)
        # Conviction multiplier: pullback 1.2x
        if 'pullback' in r['entry_type']:
            notional = min(notional*1.2, max_notional)
        # ADX strong 1.5x
        try:
            if float(r['adx_at_entry']) >= 35:
                notional = min(notional*1.5, max_notional)
        except: pass
        factor = notional / 25.0
        scaled_pnl = float(r['pnl']) * factor
        total_pnl += scaled_pnl
        equity += scaled_pnl
        equity_curve.append(equity)
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    return total_pnl, equity, max_dd, equity_curve

for label, flist in [("Baseline all", rows), ("ADX>25", adx25), ("No QQQ breakout", no_qqq_breakout), ("SPY only", spy_only), ("ADX>25 OR pullback", filtered_pullback_keep)]:
    for start_eq in [25000, 50000, 100000]:
        for pct in [0.20]:
            total, end_eq, dd, curve = simulate_percent_trades(flist, start_equity=start_eq, pct=pct, max_notional=20000, compounding=True)
            ret_pct = total/start_eq*100
            print(f"{label} pct {pct*100:.0f}% start ${start_eq}: total PnL ${total:.2f} end ${end_eq:.2f} ({ret_pct:.2f}% over {len(flist)} trades period) maxDD ${dd:.2f}")
            # also show if not compounding (fixed % of initial)
            # Save curve for best
            if label=="No QQQ breakout" and start_eq==50000:
                # export
                # curve export simplified
                curve_path = OUT_DIR / f"equity_curve_{label.replace(' ', '_')}_{start_eq}.csv"
                with open(curve_path, "w", newline="") as cf:
                    import csv as csv2
                    cw=csv2.writer(cf)
                    cw.writerow(["trade_index","equity"])
                    for i,e in enumerate(curve):
                        cw.writerow([i, f"{e:.2f}"])

# Also simulate fixed large notional
def simulate_fixed(trades, notional=5000):
    total = sum(float(r['pnl'])*notional/25 for r in trades)
    return total

print("\nFixed notional comparison (no compounding):")
for label, flist in [("Baseline", rows), ("ADX>25", adx25), ("No QQQ breakout", no_qqq_breakout), ("SPY only", spy_only)]:
    for notion in [5000,10000,20000]:
        print(f"{label} at ${notion}: ${simulate_fixed(flist, notion):.2f}")

# Generate scaled trades CSV for best filter with 5000 fixed and 10k percent
import csv as csvm
best = no_qqq_breakout
# Fixed 5000
with open(OUT_DIR / "scaled_trades_fixed_5000.csv","w",newline="") as f:
    w=csv.writer(f)
    w.writerow(["symbol","entry_time","entry_price","exit_price","pnl_25","pnl_scaled","qty_25","qty_scaled","entry_type","adx","notional"])
    for r in best:
        factor=5000/25
        w.writerow([r['symbol'],r['entry_time'],r['entry_price'],r['exit_price'],r['pnl'],float(r['pnl'])*factor,r['qty'],float(r['qty'])*factor,r['entry_type'],r['adx_at_entry'],5000])

# Percent 50k
equity=50000
pct=0.20
max_n=20000
with open(OUT_DIR / "scaled_trades_percent_50k_20pct.csv","w",newline="") as f:
    w=csv.writer(f)
    w.writerow(["symbol","entry_time","entry_price","exit_price","pnl_scaled","notional","equity_after","entry_type","adx"])
    cur_eq=equity
    for r in best:
        notion = min(cur_eq*pct, max_n)
        if 'pullback' in r['entry_type']:
            notion = min(notion*1.2, max_n)
        try:
            if float(r['adx_at_entry'])>=35:
                notion=min(notion*1.5,max_n)
        except: pass
        scaled=float(r['pnl'])*notion/25
        cur_eq+=scaled
        w.writerow([r['symbol'],r['entry_time'],r['entry_price'],r['exit_price'],f"{scaled:.4f}",f"{notion:.2f}",f"{cur_eq:.2f}",r['entry_type'],r['adx_at_entry']])

print("\nSimulation artifacts written to", OUT_DIR)
