import csv
import os
import yfinance as yf
import pandas as pd
import requests

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
TICKERS = ["DUOL", "RDDT", "HIMS"]
STRATEGIES = ["v1.0", "v2.0"]  # v1.0 = Fixed 5d | v2.0 = Close > Prev High/Low
SHOCK_THRESH = 3.5
ATR_MULT = 2.0
HOLD_DAYS = 5
SEND_NO_SIGNAL = True
LOG_FILE = "trades.csv"

COLUMNS = [
    "Ticker", "Strategy", "SignalDate", "Side", "Status", "EntryDate",
    "EntryPrice", "StopPrice", "ExitDate", "ExitPrice", "DaysHeld",
    "ReturnPct", "LastUpdated"
]

def load_trades():
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, mode="r", newline="") as f:
        rows = list(csv.DictReader(f))

    for r in rows:
        if not r.get("Ticker"):
            r["Ticker"] = "DUOL"
        if not r.get("Strategy"):
            r["Strategy"] = "v1.0"

    existing_keys = {(r["Ticker"], r["Strategy"], r["SignalDate"]) for r in rows}
    cloned = []
    for r in rows:
        if r["Strategy"] == "v1.0" and r["Status"] == "PENDING":
            key_v2 = (r["Ticker"], "v2.0", r["SignalDate"])
            if key_v2 not in existing_keys:
                new_r = dict(r)
                new_r["Strategy"] = "v2.0"
                cloned.append(new_r)
                existing_keys.add(key_v2)
    rows.extend(cloned)
    return rows

def save_trades(trades):
    with open(LOG_FILE, mode="w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(trades)

def calc_return(side, entry, exit_p):
    if side == "LONG":
        return ((exit_p - entry) / entry) * 100
    return ((entry - exit_p) / entry) * 100

def format_stats(trades):
    lines = ["\n📊 **Forward-Test Scoreboard (All Tickers):**"]
    for strat, label in [("v1.0", "v1.0 Fixed 5d"), ("v2.0", "v2.0 Prev H/L")]:
        closed = [t for t in trades if t["Strategy"] == strat and t["Status"].startswith("CLOSED")]
        if not closed:
            lines.append(f"• **{label}:** 0 closed trades")
        else:
            rets = [float(t["ReturnPct"]) for t in closed]
            wins = sum(1 for r in rets if r > 0)
            wr = (wins / len(rets)) * 100
            cum = sum(rets)
            lines.append(f"• **{label}:** {len(closed)} closed | WR: {wr:.1f}% | Sum: {cum:+.2f}%")
    return "\n".join(lines)

def manage_trade(t, sym, strat, date_str, open_p, high_p, low_p, close_p, prev_high, prev_low):
    side = t["Side"]
    stop_p = float(t["StopPrice"])

    if t["LastUpdated"] == date_str:
        if t["Status"] == "PENDING":
            action = "Buy" if side == "LONG" else "Short"
            exit_rule = f"hold {HOLD_DAYS}d" if strat == "v1.0" else "Prev H/L exit"
            msg = (
                f"  • `{strat}`: 🟡 **PENDING {side}** — "
                f"{action} next Open ({exit_rule}) | Stop: ~${stop_p:.2f}"
            )
            return msg, False
        entry_p = float(t["EntryPrice"])
        unrealized = calc_return(side, entry_p, close_p)
        hold_tag = f"Day {t['DaysHeld']}/{HOLD_DAYS}" if strat == "v1.0" else f"Day {t['DaysHeld']} (Prev H/L)"
        msg = (
            f"  • `{strat}`: ⏳ **OPEN {side}** ({hold_tag}) | "
            f"Entry: ${entry_p:.2f} (`{unrealized:+.2f}%`) | Stop: ${stop_p:.2f}"
        )
        return msg, False
