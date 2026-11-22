import os
import yfinance as yf
import pandas as pd
import requests

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
SHOCK_THRESH = 3.5
ATR_MULT = 2.0
SEND_NO_SIGNAL = True  # Change to False if you only want pings on actual setups

def check_duol():
    ticker = yf.Ticker("DUOL")
    df = ticker.history(period="6mo")

    df['Daily_Ret'] = df['Close'].pct_change() * 100
    df['RVOL'] = df['Volume'] / df['Volume'].rolling(20).mean()
    df['SMA50'] = df['Close'].rolling(50).mean()

    tr = pd.concat([
        df['High'] - df['Low'],
        (df['High'] - df['Close'].shift()).abs(),
        (df['Low'] - df['Close'].shift()).abs()
    ], axis=1).max(axis=1)
    df['ATR'] = tr.rolling(14).mean()

    latest = df.iloc[-1]
    date_str = df.index[-1].strftime('%Y-%m-%d')
    close = latest['Close']
    ret = latest['Daily_Ret']
    rvol = latest['RVOL']
    sma50 = latest['SMA50']
    atr = latest['ATR']

    near_earnings = False
    try:
        earnings = ticker.get_earnings_dates(limit=8)
        if earnings is not None and not earnings.empty:
            e_dates = pd.to_datetime(earnings.index).tz_localize(None).normalize()
            today = pd.Timestamp(date_str)
            near_earnings = any((e_dates >= today) & (e_dates <= today + pd.Timedelta(days=7)))
    except Exception:
        pass

    signal = None
    if near_earnings:
        status = f"⏸️ **EARNINGS BLACKOUT** ({date_str})\nDUOL earnings within 7 days. Skipping setups."
    elif ret < -SHOCK_THRESH and rvol < 1.5:
        stop_price = close - (ATR_MULT * atr)
        signal = "LONG"
        status = (
            f"🟢 **DUOL LONG SETUP** ({date_str})\n"
            f"• **Close:** ${close:.2f} (`{ret:+.2f}%`)\n"
            f"• **RVOL:** {rvol:.2f}x (Normal Volume ✓)\n"
            f"• **Action:** Buy at tomorrow's Open, hold 5 trading days\n"
            f"• **2x ATR Stop-Loss:** ~${stop_price:.2f} (ATR: ${atr:.2f})"
        )
    elif ret > SHOCK_THRESH and close < sma50:
        stop_price = close + (ATR_MULT * atr)
        signal = "SHORT"
        status = (
            f"🔴 **DUOL SHORT SETUP** ({date_str})\n"
            f"• **Close:** ${close:.2f} (`{ret:+.2f}%`)\n"
            f"• **Trend:** Below 50-SMA (${sma50:.2f}) ✓\n"
            f"• **Action:** Short at tomorrow's Open, hold 5 trading days\n"
            f"• **2x ATR Stop-Loss:** ~${stop_price:.2f} (ATR: ${atr:.2f})"
        )
    else:
        status = (
            f"⚪ **DUOL Daily Check ({date_str}): No Setup**\n"
            f"Close: ${close:.2f} (`{ret:+.2f}%`) | RVOL: {rvol:.2f}x | 50-SMA: ${sma50:.2f}"
        )

    print(status)
    if signal or SEND_NO_SIGNAL:
        resp = requests.post(WEBHOOK_URL, json={"content": status})
        resp.raise_for_status()

if __name__ == "__main__":
    check_duol()
