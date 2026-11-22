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

    # Auto-migrate older trades.csv rows
    for r in rows:
        if not r.get("Ticker"):
            r["Ticker"] = "DUOL"
        if not r.get("Strategy"):
            r["Strategy"] = "v1.0"

    # Clone any existing PENDING v1.0 setups into v2.0 so Monday fills both
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
    if t["LastUpdated"] == date_str:
            side = t["Side"]
            stop_p = float(t["StopPrice"])
            if t["Status"] == "PENDING":
                action = "Buy" if side == "LONG" else "Short"
                exit_rule = f"hold {HOLD_DAYS}d" if strat == "v1.0" else "Prev H/L exit"
                return f"  • `{strat}`: 🟡 **PENDING {side}** — {action} next Open ({exit_rule}) | Stop: ~${stop_p:.2f}", False
            entry_p = float(t["EntryPrice"])
            unrealized = calc_return(side, entry_p, close_p)
            hold_tag = f"Day {t['DaysHeld']}/{HOLD_DAYS}" if strat == "v1.0" else f"Day {t['DaysHeld']} (Prev H/L)"
            return f"  • `{strat}`: ⏳ **OPEN {side}** ({hold_tag}) | Entry: ${entry_p:.2f} (`{unrealized:+.2f}%`) | Stop: ${stop_p:.2f}", False
    
        side = t["Side"]
        stop_p = float(t["StopPrice"])
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

    # 1. Check 2x ATR Stop-Loss (both strategies)
    stopped = (side == "LONG" and low_p <= stop_p) or (side == "SHORT" and high_p >= stop_p)
    if stopped:
        exit_p = min(open_p, stop_p) if side == "LONG" else max(open_p, stop_p)
        r_pct = calc_return(side, entry_p, exit_p)
        t["Status"] = "CLOSED_STOP"
        t["ExitDate"] = date_str
        t["ExitPrice"] = f"{exit_p:.2f}"
        t["ReturnPct"] = f"{r_pct:.2f}"
        t["LastUpdated"] = date_str
        return f"  • `{strat}`: {prefix}🛑 STOPPED ({side}) @ ${exit_p:.2f} (`{r_pct:+.2f}%`)", True

    # 2. Check Strategy-Specific Exit
    if strat == "v1.0" and days_held >= HOLD_DAYS:
        r_pct = calc_return(side, entry_p, close_p)
        t["Status"] = "CLOSED_TIME"
        t["ExitDate"] = date_str
        t["ExitPrice"] = f"{close_p:.2f}"
        t["ReturnPct"] = f"{r_pct:.2f}"
        t["LastUpdated"] = date_str
        return f"  • `{strat}`: {prefix}✅ CLOSED Day {HOLD_DAYS} ({side}) @ ${close_p:.2f} (`{r_pct:+.2f}%`)", True

    if strat == "v2.0":
        pa_exit = (side == "LONG" and close_p > prev_high) or (side == "SHORT" and close_p < prev_low)
        if pa_exit:
            r_pct = calc_return(side, entry_p, close_p)
            t["Status"] = "CLOSED_PA"
            t["ExitDate"] = date_str
            t["ExitPrice"] = f"{close_p:.2f}"
            t["ReturnPct"] = f"{r_pct:.2f}"
            t["LastUpdated"] = date_str
            lvl = prev_high if side == "LONG" else prev_low
            return f"  • `{strat}`: {prefix}✅ CLOSED Prev H/L (${lvl:.2f}) @ ${close_p:.2f} (`{r_pct:+.2f}%`)", True

    # 3. Still Holding
    unrealized = calc_return(side, entry_p, close_p)
    t["LastUpdated"] = date_str
    hold_tag = f"Day {days_held}/{HOLD_DAYS}" if strat == "v1.0" else f"Day {days_held} (Prev H/L exit)"
    return f"  • `{strat}`: {prefix}⏳ Holding {side} ({hold_tag}) | `{unrealized:+.2f}%` | Stop: ${stop_p:.2f}", important

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
    prev = df.iloc[-2]
    date_str = df.index[-1].strftime("%Y-%m-%d")

    open_p = float(latest["Open"])
    high_p = float(latest["High"])
    low_p = float(latest["Low"])
    close_p = float(latest["Close"])
    prev_high = float(prev["High"])
    prev_low = float(prev["Low"])
    ret = float(latest["Daily_Ret"])
    rvol = float(latest["RVOL"])
    sma50 = float(latest["SMA50"])
    atr = float(latest["ATR"])

    # Check earnings blackout once per ticker
    near_earnings = False
    try:
        earnings = ticker.get_earnings_dates(limit=8)
        if earnings is not None and not earnings.empty:
            e_dates = pd.to_datetime(earnings.index).tz_localize(None).normalize()
            today = pd.Timestamp(date_str)
            near_earnings = any((e_dates >= today) & (e_dates <= today + pd.Timedelta(days=7)))
    except Exception:
        pass

    # Evaluate signal once per ticker
    new_side = None
    if not near_earnings:
        if ret < -SHOCK_THRESH and rvol < 1.5:
            new_side = "LONG"
        elif ret > SHOCK_THRESH and close_p < sma50:
            new_side = "SHORT"

    lines = [f"**{sym} ({date_str})** — Close: ${close_p:.2f} (`{ret:+.2f}%`) | RVOL: {rvol:.2f}x"]
    any_imp = False

    for strat in STRATEGIES:
        active_idx = next(
            (i for i, t in enumerate(trades)
             if t["Ticker"] == sym and t["Strategy"] == strat and t["Status"] in ("PENDING", "OPEN")),
            None
        )

        if active_idx is not None:
            msg, imp = manage_trade(
                trades[active_idx], sym, strat, date_str,
                open_p, high_p, low_p, close_p, prev_high, prev_low
            )
            lines.append(msg)
            any_imp = any_imp or imp
        else:
            if near_earnings:
                lines.append(f"  • `{strat}`: ⏸️ Earnings Blackout")
            elif new_side:
                stop_p = close_p - (ATR_MULT * atr) if new_side == "LONG" else close_p + (ATR_MULT * atr)
                trades.append({
                    "Ticker": sym, "Strategy": strat, "SignalDate": date_str,
                    "Side": new_side, "Status": "PENDING", "EntryDate": "",
                    "EntryPrice": "", "StopPrice": f"{stop_p:.2f}",
                    "ExitDate": "", "ExitPrice": "", "DaysHeld": "0",
                    "ReturnPct": "", "LastUpdated": date_str
                })
                icon = "🟢" if new_side == "LONG" else "🔴"
                lines.append(f"  • `{strat}`: {icon} **{new_side} SETUP** — Buy/Short next Open | Stop: ~${stop_p:.2f}")
                any_imp = True
            else:
                lines.append(f"  • `{strat}`: ⚪ Flat (No Setup)")

    return "\n".join(lines), any_imp

def run_scanner():
    trades = load_trades()
    sections = []
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
