import csv
import os
import yfinance as yf
import pandas as pd
import requests

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
TICKERS = ["DUOL", "RDDT", "HIMS"]
SHOCK_THRESH = 3.5
ATR_MULT = 2.0
HOLD_DAYS = 5
SEND_NO_SIGNAL = True
LOG_FILE = "trades.csv"

COLUMNS = [
    "Ticker", "SignalDate", "Side", "Status", "EntryDate", "EntryPrice",
    "StopPrice", "ExitDate", "ExitPrice", "DaysHeld", "ReturnPct", "LastUpdated"
]

def load_trades():
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, mode="r", newline="") as f:
        rows = list(csv.DictReader(f))
    # Auto-migrate older trades.csv rows that lacked a Ticker column
    for r in rows:
        if "Ticker" not in r or not r["Ticker"]:
            r["Ticker"] = "DUOL"
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
    closed = [t for t in trades if t["Status"].startswith("CLOSED")]
    if not closed:
        return "\n📊 **Forward-Test Log:** 0 closed trades so far."
    rets = [float(t["ReturnPct"]) for t in closed]
    wins = sum(1 for r in rets if r > 0)
    win_rate = (wins / len(rets)) * 100
    cum_ret = sum(rets)
    return (
        f"\n📊 **Forward-Test Log (All Tickers):** {len(closed)} closed | "
        f"Win Rate: {win_rate:.1f}% | Sum Return: {cum_ret:+.2f}%"
    )

def process_ticker(sym, trades):
    ticker = yf.Ticker(sym)
    df = ticker.history(period="6mo")

    df["Daily_Ret"] = df["Close"].pct_change() * 100
    df["RVOL"] = df["Volume"] / df["Volume"].rolling(20).mean()
    df["SMA50"] = df["Close"].rolling(50).mean()

    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift()).abs(),
        (df["Low"] - df["Close"].shift()).abs()
    ], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(14).mean()

    latest = df.iloc[-1]
    date_str = df.index[-1].strftime("%Y-%m-%d")
    open_p = float(latest["Open"])
    high_p = float(latest["High"])
    low_p = float(latest["Low"])
    close_p = float(latest["Close"])
    ret = float(latest["Daily_Ret"])
    rvol = float(latest["RVOL"])
    sma50 = float(latest["SMA50"])
    atr = float(latest["ATR"])

    active_idx = next(
        (i for i, t in enumerate(trades) if t["Ticker"] == sym and t["Status"] in ("PENDING", "OPEN")),
        None
    )

    lines = []
    important = False
    acted_today = False

    # 1. Manage existing PENDING or OPEN trade for this ticker
    if active_idx is not None:
        t = trades[active_idx]
        if t["LastUpdated"] == date_str:
            lines.append(
                f"ℹ️ **{sym} ({date_str}):** Already processed today. "
                f"Active `{t['Side']}` is `{t['Status']}` (Day {t['DaysHeld']}/{HOLD_DAYS})."
            )
            acted_today = True
        else:
            side = t["Side"]
            stop_p = float(t["StopPrice"])

            if t["Status"] == "PENDING":
                t["Status"] = "OPEN"
                t["EntryDate"] = date_str
                t["EntryPrice"] = f"{open_p:.2f}"
                t["DaysHeld"] = "1"
                lines.append(
                    f"▶️ **{sym} FILLED {side}** ({date_str}) at Open **${open_p:.2f}** \vert{} Stop:${stop_p:.2f}"
                )
                important = True
            else:
                t["DaysHeld"] = str(int(t["DaysHeld"]) + 1)

            entry_p = float(t["EntryPrice"])
            days_held = int(t["DaysHeld"])

            stopped = (side == "LONG" and low_p <= stop_p) or (side == "SHORT" and high_p >= stop_p)
            if stopped:
                exit_p = min(open_p, stop_p) if side == "LONG" else max(open_p, stop_p)
                r_pct = calc_return(side, entry_p, exit_p)
                t["Status"] = "CLOSED_STOP"
                t["ExitDate"] = date_str
                t["ExitPrice"] = f"{exit_p:.2f}"
                t["ReturnPct"] = f"{r_pct:.2f}"
                lines.append(
                    f"🛑 **{sym} STOPPED OUT ({side})** on {date_str}\n"
                    f"• Entry: ${entry_p:.2f} → Exit: ${exit_p:.2f} (`{r_pct:+.2f}%`)"
                )
                important = True
            elif days_held >= HOLD_DAYS:
                r_pct = calc_return(side, entry_p, close_p)
                t["Status"] = "CLOSED_TIME"
                t["ExitDate"] = date_str
                t["ExitPrice"] = f"{close_p:.2f}"
                t["ReturnPct"] = f"{r_pct:.2f}"
                lines.append(
                    f"✅ **{sym} CLOSED ({side} — Day {HOLD_DAYS})** on {date_str}\n"
                    f"• Entry: ${entry_p:.2f} → Exit: ${close_p:.2f} (`{r_pct:+.2f}%`)"
                )
                important = True
            else:
                unrealized = calc_return(side, entry_p, close_p)
                lines.append(
                    f"⏳ **{sym} HOLDING {side}** (Day {days_held}/{HOLD_DAYS}) | "
                    f"Close: ${close_p:.2f} (`{unrealized:+.2f}\%`) \vert{} Stop: ${stop_p:.2f}"
                )

            t["LastUpdated"] = date_str
            acted_today = True

    # 2. If flat, scan for a new setup
    if not acted_today:
        near_earnings = False
        try:
            earnings = ticker.get_earnings_dates(limit=8)
            if earnings is not None and not earnings.empty:
                e_dates = pd.to_datetime(earnings.index).tz_localize(None).normalize()
                today = pd.Timestamp(date_str
