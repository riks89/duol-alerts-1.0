import csv
import os
import yfinance as yf
import pandas as pd
import requests

WEBHOOK_URL = os.environ.get("UBER_DISCORD_WEBHOOK_URL") or os.environ["DISCORD_WEBHOOK_URL"]
TICKERS = ["UBER", "BKNG", "ABNB"]
STRATEGIES = ["v1.0", "v2.0"]  # v1.0 = Fixed 10d | v2.0 = Exit RSI >= 50
RSI_ENTRY = 30.0
RSI_EXIT = 50.0
ATR_MULT = 3.0
HOLD_DAYS = 10
SEND_NO_SIGNAL = True
LOG_FILE = "uber_trades.csv"

COLUMNS = [
    "Ticker", "Strategy", "SignalDate", "Side", "Status", "EntryDate",
    "EntryPrice", "StopPrice", "ExitDate", "ExitPrice", "DaysHeld",
    "ReturnPct", "LastUpdated"
]

def load_trades():
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, mode="r", newline="") as f:
        return list(csv.DictReader(f))

def save_trades(trades):
    with open(LOG_FILE, mode="w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(trades)

def calc_return(entry, exit_p):
    return ((exit_p - entry) / entry) * 100

def format_stats(trades):
    lines = ["\n📊 **UBER Range Forward-Test Scoreboard:**"]
    for strat, label in [("v1.0", "v1.0 Fixed 10d"), ("v2.0", "v2.0 RSI >= 50")]:
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

def manage_trade(t, strat, date_str, open_p, low_p, close_p, rsi14):
    stop_p = float(t["StopPrice"])

    if t["LastUpdated"] == date_str:
        if t["Status"] == "PENDING":
            exit_rule = f"hold {HOLD_DAYS}d" if strat == "v1.0" else f"RSI >= {int(RSI_EXIT)} exit"
            return f"  • `{strat}`: 🟡 **PENDING LONG** — Buy next Open ({exit_rule}) | Stop: ~${stop_p:.2f}", False
        entry_p = float(t["EntryPrice"])
        unrealized = calc_return(entry_p, close_p)
        hold_tag = f"Day {t['DaysHeld']}/{HOLD_DAYS}" if strat == "v1.0" else f"Day {t['DaysHeld']} (RSI: {rsi14:.1f})"
        return f"  • `{strat}`: ⏳ **OPEN LONG** ({hold_tag}) | Entry: ${entry_p:.2f} (`{unrealized:+.2f}%`) | Stop: ${stop_p:.2f}", False

    important = False
    prefix = ""

    if t["Status"] == "PENDING":
        t["Status"] = "OPEN"
        t["EntryDate"] = date_str
        t["EntryPrice"] = f"{open_p:.2f}"
        t["DaysHeld"] = "1"
        prefix = f"▶️ Filled @ ${open_p:.2f} → "
        important = True
    else:
        t["DaysHeld"] = str(int(t["DaysHeld"]) + 1)

    entry_p = float(t["EntryPrice"])
    days_held = int(t["DaysHeld"])

    # 1. Check 3x ATR Stop-Loss
    if low_p <= stop_p:
        exit_p = min(open_p, stop_p)
        r_pct = calc_return(entry_p, exit_p)
        t["Status"] = "CLOSED_STOP"
        t["ExitDate"] = date_str
        t["ExitPrice"] = f"{exit_p:.2f}"
        t["ReturnPct"] = f"{r_pct:.2f}"
        t["LastUpdated"] = date_str
        return f"  • `{strat}`: {prefix}🛑 STOPPED (LONG) @ ${exit_p:.2f} (`{r_pct:+.2f}%`)", True

    # 2. Check Strategy-Specific Exit
    if strat == "v1.0" and days_held >= HOLD_DAYS:
        r_pct = calc_return(entry_p, close_p)
        t["Status"] = "CLOSED_TIME"
        t["ExitDate"] = date_str
        t["ExitPrice"] = f"{close_p:.2f}"
        t["ReturnPct"] = f"{r_pct:.2f}"
        t["LastUpdated"] = date_str
        return f"  • `{strat}`: {prefix}✅ CLOSED Day {HOLD_DAYS} @ ${close_p:.2f} (`{r_pct:+.2f}%`)", True

    if strat == "v2.0" and rsi14 >= RSI_EXIT:
        r_pct = calc_return(entry_p, close_p)
        t["Status"] = "CLOSED_RSI50"
        t["ExitDate"] = date_str
        t["ExitPrice"] = f"{close_p:.2f}"
        t["ReturnPct"] = f"{r_pct:.2f}"
        t["LastUpdated"] = date_str
        return f"  • `{strat}`: {prefix}✅ CLOSED RSI={rsi14:.1f} @ ${close_p:.2f} (`{r_pct:+.2f}%`)", True

    # 3. Still Holding
    unrealized = calc_return(entry_p, close_p)
    t["LastUpdated"] = date_str
    hold_tag = f"Day {days_held}/{HOLD_DAYS}" if strat == "v1.0" else f"Day {days_held} (RSI: {rsi14:.1f})"
    return f"  • `{strat}`: {prefix}⏳ Holding LONG ({hold_tag}) | `{unrealized:+.2f}%` | Stop: ${stop_p:.2f}", important

def process_ticker(sym, trades):
    ticker = yf.Ticker(sym)
    df = ticker.history(period="6mo")

    df["Daily_Ret"] = df["Close"].pct_change() * 100
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift()).abs(),
        (df["Low"] - df["Close"].shift()).abs()
    ], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(14).mean()

    delta = df["Close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    df["RSI14"] = 100 - (100 / (1 + gain / loss))

    latest = df.iloc[-1]
    date_str = df.index[-1].strftime("%Y-%m-%d")

    open_p = float(latest["Open"])
    low_p = float(latest["Low"])
    close_p = float(latest["Close"])
    ret = float(latest["Daily_Ret"])
    atr = float(latest["ATR"])
    rsi14 = float(latest["RSI14"])

    # Check 7-day earnings blackout
    near_earnings = False
    try:
        earnings = ticker.get_earnings_dates(limit=8)
        if earnings is not None and not earnings.empty:
            e_dates = pd.to_datetime(earnings.index).tz_localize(None).normalize()
            today = pd.Timestamp(date_str)
            near_earnings = any((e_dates >= today) & (e_dates <= today + pd.Timedelta(days=7)))
    except Exception:
        pass

    lines = [f"**{sym} ({date_str})** — Close: ${close_p:.2f} (`{ret:+.2f}%`) | RSI(14): {rsi14:.1f}"]
    any_imp = False

    for strat in STRATEGIES:
        active_idx = next(
            (i for i, t in enumerate(trades)
             if t["Ticker"] == sym and t["Strategy"] == strat and t["Status"] in ("PENDING", "OPEN")),
            None
        )

        if active_idx is not None:
            msg, imp = manage_trade(
                trades[active_idx], strat, date_str, open_p, low_p, close_p, rsi14
            )
            lines.append(msg)
            any_imp = any_imp or imp
        else:
            if near_earnings:
                lines.append(f"  • `{strat}`: ⏸️ Earnings Blackout")
            elif rsi14 < RSI_ENTRY:
                stop_p = close_p - (ATR_MULT * atr)
                trades.append({
                    "Ticker": sym, "Strategy": strat, "SignalDate": date_str,
                    "Side": "LONG", "Status": "PENDING", "EntryDate": "",
                    "EntryPrice": "", "StopPrice": f"{stop_p:.2f}",
                    "ExitDate": "", "ExitPrice": "", "DaysHeld": "0",
                    "ReturnPct": "", "LastUpdated": date_str
                })
                exit_rule = f"hold {HOLD_DAYS}d" if strat == "v1.0" else f"RSI >= {int(RSI_EXIT)} exit"
                lines.append(f"  • `{strat}`: 🟢 **LONG SETUP** — Buy next Open ({exit_rule}) | 3x ATR Stop: ~${stop_p:.2f}")
                any_imp = True
            else:
                lines.append(f"  • `{strat}`: ⚪ Flat (No Setup)")

    return "\n".join(lines), any_imp

def run_scanner():
    trades = load_trades()
    sections = ["📈 **RANGE REVERSION SCANNER (UBER / BKNG / ABNB)**"]
    any_important = False

    for sym in TICKERS:
        try:
            msg, imp = process_ticker(sym, trades)
            sections.append(msg)
            any_important = any_important or imp
        except Exception as e:
            sections.append(f"⚠️ **{sym} Error:** {e}")

    save_trades(trades)
    final_msg = "\n\n".join(sections) + "\n" + format_stats(trades)
    print(final_msg)

    if any_important or SEND_NO_SIGNAL:
        resp = requests.post(WEBHOOK_URL, json={"content": final_msg})
        resp.raise_for_status()

if __name__ == "__main__":
    run_scanner()
