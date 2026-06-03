import oandapyV20
import oandapyV20.endpoints.instruments as instruments
import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.trades as trades_endpoint
import pandas as pd
import ta
import time
import requests
import gspread
from google.oauth2.service_account import Credentials
from config import API_KEY, ACCOUNT_ID, DISCORD_WEBHOOK, TRADE_UNITS, STOP_LOSS_PCT, TAKE_PROFIT_PCT, INSTRUMENTS, GOOGLE_SHEET_NAME, GOOGLE_CREDS_FILE

client = oandapyV20.API(access_token=API_KEY, environment="practice")

open_trades = {}

# ─── Google Sheets setup ──────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_HEADERS = [
    "Trade ID", "Pair", "Side", "Indicators", "Entry Price",
    "Stop Loss", "Take Profit", "Units", "Open Time",
    "Close Price", "Close Time", "P&L", "Result"
]

def get_sheet():
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open(GOOGLE_SHEET_NAME)
    ws = sh.sheet1
    if ws.row_values(1) != SHEET_HEADERS:
        ws.clear()
        ws.append_row(SHEET_HEADERS)
    return ws

def log_trade_open(trade_id, instrument, side, reasons, fill_price, sl, tp):
    """Append a new row when a trade opens. Close columns left blank."""
    try:
        ws = get_sheet()
        row = [
            trade_id,
            instrument,
            side.upper(),
            ", ".join(reasons),
            fill_price,
            sl,
            tp,
            TRADE_UNITS,
            pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
            "",   # Close Price (filled later)
            "",   # Close Time  (filled later)
            "",   # P&L         (filled later)
            "",   # Result       (filled later)
        ]
        ws.append_row(row)
        print(f"[Sheets] Logged open trade {trade_id}")
    except Exception as e:
        print(f"[Sheets] Error logging open trade: {e}")

def update_trade_close(trade_id, close_price, pl):
    """Find the row for trade_id and fill in close columns."""
    try:
        ws = get_sheet()
        col_a = ws.col_values(1)
        if str(trade_id) not in col_a:
            print(f"[Sheets] Trade {trade_id} not found in sheet")
            return

        row_idx = col_a.index(str(trade_id)) + 1

        result = "WIN ✅" if float(pl) >= 0 else "LOSS ❌"

        ws.update_cell(row_idx, 10, close_price)
        ws.update_cell(row_idx, 11, pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"))
        ws.update_cell(row_idx, 12, pl)
        ws.update_cell(row_idx, 13, result)
        print(f"[Sheets] Updated close for trade {trade_id} → {result}")
    except Exception as e:
        print(f"[Sheets] Error updating close: {e}")

# ─── Helpers ──────────────────────────────────────────────────────────────────

def send_discord(message):
    data = {"content": message}
    requests.post(DISCORD_WEBHOOK, json=data)

def get_decimals(instrument):
    return 2 if "JPY" in instrument else 5

def get_candles(instrument, count=100, granularity="M15"):
    params = {"count": count, "granularity": granularity, "price": "M"}
    r = instruments.InstrumentsCandles(instrument, params=params)
    client.request(r)

    data = []
    for c in r.response["candles"]:
        if c["complete"]:
            data.append({
                "time": c["time"],
                "open":   float(c["mid"]["o"]),
                "high":   float(c["mid"]["h"]),
                "low":    float(c["mid"]["l"]),
                "close":  float(c["mid"]["c"]),
                "volume": c["volume"],
            })

    df = pd.DataFrame(data)
    df["time"] = pd.to_datetime(df["time"])
    df.set_index("time", inplace=True)
    return df

def add_indicators(df):
    df["rsi"] = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
    macd = ta.trend.MACD(df["close"])
    df["macd_hist"] = macd.macd_diff()
    df["obv"] = ta.volume.OnBalanceVolumeIndicator(df["close"], df["volume"]).on_balance_volume()
    return df

def detect_divergence(df, lookback=5):
    last = df.iloc[-1]
    prev = df.iloc[-1 - lookback]

    score_buy = score_sell = 0
    reasons = []

    if last["close"] < prev["close"]:
        if last["rsi"]       > prev["rsi"]:       score_buy += 1; reasons.append("RSI bullish divergence")
        if last["macd_hist"] > prev["macd_hist"]: score_buy += 1; reasons.append("MACD bullish divergence")
        if last["obv"]       > prev["obv"]:       score_buy += 1; reasons.append("OBV bullish divergence")

    if last["close"] > prev["close"]:
        if last["rsi"]       < prev["rsi"]:       score_sell += 1; reasons.append("RSI bearish divergence")
        if last["macd_hist"] < prev["macd_hist"]: score_sell += 1; reasons.append("MACD bearish divergence")
        if last["obv"]       < prev["obv"]:       score_sell += 1; reasons.append("OBV bearish divergence")

    return score_buy, score_sell, reasons

def place_order(instrument, side, units, current_price):
    decimals = get_decimals(instrument)
    sl_distance = current_price * STOP_LOSS_PCT
    tp_distance = current_price * TAKE_PROFIT_PCT

    if side == "buy":
        sl = round(current_price - sl_distance, decimals)
        tp = round(current_price + tp_distance, decimals)
        order_units = units
    else:
        sl = round(current_price + sl_distance, decimals)
        tp = round(current_price - tp_distance, decimals)
        order_units = -units

    data = {
        "order": {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(order_units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "takeProfitOnFill": {"price": str(tp)},
            "stopLossOnFill":   {"price": str(sl)},
        }
    }

    try:
        r = orders.OrderCreate(ACCOUNT_ID, data=data)
        client.request(r)
        print(f"Order response: {r.response}")
        return r.response
    except Exception as e:
        print(f"Place order failed ERROR: {e}")
        print(f"Order data sent: {data}")
        return {}

# ─── Trade monitoring ──────────────────────────────────────────────────────────

def check_open_trades():
    closed = []

    for trade_id, info in open_trades.items():
        try:
            r = trades_endpoint.TradeDetails(ACCOUNT_ID, trade_id)
            client.request(r)
            trade = r.response["trade"]

            if trade["state"] == "CLOSED":
                entry       = float(info["entry"])
                close_price = float(trade["averageClosePrice"])
                pl          = float(trade["realizedPL"])
                side        = info["side"]
                instrument  = info["instrument"]

                if side == "buy":
                    result = "✅ TAKE PROFIT HIT" if close_price >= entry + (entry * TAKE_PROFIT_PCT * 0.9) else "❌ STOP LOSS HIT"
                else:
                    result = "✅ TAKE PROFIT HIT" if close_price <= entry - (entry * TAKE_PROFIT_PCT * 0.9) else "❌ STOP LOSS HIT"

                msg = (
                    f"{result}\n"
                    f"Pair: {instrument}\n"
                    f"Trade ID: {trade_id}\n"
                    f"Side: {side.upper()}\n"
                    f"Entry: {entry}\n"
                    f"Close Price: {close_price}\n"
                    f"P&L: ${pl}"
                )
                print(msg)
                send_discord(msg)

                update_trade_close(trade_id, close_price, pl)

                closed.append(trade_id)

        except Exception as e:
            print(f"Error checking trade {trade_id}: {e}")

    for trade_id in closed:
        del open_trades[trade_id]

# ─── Main loop ─────────────────────────────────────────────────────────────────

def run_bot():
    print("Bot started, checking every 15 minutes...")
    send_discord(f"🤖 **Bot Started** — {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}")

    while True:
        try:
            if open_trades:
                check_open_trades()

            for instrument in INSTRUMENTS:
                try:
                    print(f"\n========== {instrument} ==========")
                    df = get_candles(instrument)
                    df = add_indicators(df)

                    last = df.iloc[-1]
                    prev = df.iloc[-6]

                    print(f"Time: {pd.Timestamp.now()}")
                    print(f"Last close: {last['close']} | Prev close: {prev['close']}")
                    print(f"Last RSI:   {last['rsi']:.2f} | Prev RSI:   {prev['rsi']:.2f}")
                    print(f"Last MACD:  {last['macd_hist']:.6f} | Prev MACD:  {prev['macd_hist']:.6f}")
                    print(f"Last OBV:   {last['obv']} | Prev OBV:   {prev['obv']}")

                    score_buy, score_sell, reasons = detect_divergence(df)
                    print(f"Buy score: {score_buy}/3 | Sell score: {score_sell}/3")
                    print(f"Reasons: {reasons}")

                    def strength_label(score):
                        return {1: "⚠️ Weak (1/3)", 2: "🔶 Medium (2/3)", 3: "💪 Strong (3/3)"}.get(score, "")

                    if score_buy >= 1:
                        # Skip if 1/3 and the only signal is OBV bullish (33% win rate — not worth it)
                        if score_buy == 1 and reasons == ["OBV bullish divergence"]:
                            print("SIGNAL: Skipping — OBV bullish only (filtered, 33% win rate)")
                            time.sleep(1)
                            continue

                        print(f"SIGNAL: BUY 🟢 ({score_buy}/3)")
                        response = place_order(instrument, "buy", TRADE_UNITS, last["close"])

                        if not response or "orderFillTransaction" not in response:
                            print(f"Order not filled for {instrument} — skipping"); time.sleep(1); continue
                        if "tradeOpened" not in response["orderFillTransaction"]:
                            print(f"No trade opened for {instrument} — skipping"); time.sleep(1); continue

                        trade_id   = response["orderFillTransaction"]["tradeOpened"]["tradeID"]
                        fill_price = float(response["orderFillTransaction"]["price"])
                        decimals   = get_decimals(instrument)
                        sl = round(fill_price - fill_price * STOP_LOSS_PCT, decimals)
                        tp = round(fill_price + fill_price * TAKE_PROFIT_PCT, decimals)

                        open_trades[trade_id] = {"side": "buy", "entry": fill_price, "instrument": instrument}

                        log_trade_open(trade_id, instrument, "buy", reasons, fill_price, sl, tp)

                        msg = (
                            f"🟢 **BUY — {instrument}**\n"
                            f"Time: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}\n"
                            f"Trade ID: {trade_id}\n"
                            f"Entry Price: {fill_price}\n"
                            f"Stop Loss: {sl}\n"
                            f"Take Profit: {tp}\n"
                            f"Units: {TRADE_UNITS}\n"
                            f"RSI: {last['rsi']:.2f}\n"
                            f"Signal Strength: {strength_label(score_buy)}\n"
                            f"Confirmed by: {', '.join(reasons)}"
                        )
                        send_discord(msg)

                    elif score_sell >= 1:
                        # Skip if 1/3 and the only signal is MACD bearish (57% — decent but below threshold for solo trades)
                        if score_sell == 1 and reasons == ["MACD bearish divergence"]:
                            print("SIGNAL: Skipping — MACD bearish only (filtered, 57% win rate)")
                            time.sleep(1)
                            continue

                        print(f"SIGNAL: SELL 🔴 ({score_sell}/3)")
                        response = place_order(instrument, "sell", TRADE_UNITS, last["close"])

                        if not response or "orderFillTransaction" not in response:
                            print(f"Order not filled for {instrument} — skipping"); time.sleep(1); continue
                        if "tradeOpened" not in response["orderFillTransaction"]:
                            print(f"No trade opened for {instrument} — skipping"); time.sleep(1); continue

                        trade_id   = response["orderFillTransaction"]["tradeOpened"]["tradeID"]
                        fill_price = float(response["orderFillTransaction"]["price"])
                        decimals   = get_decimals(instrument)
                        sl = round(fill_price + fill_price * STOP_LOSS_PCT, decimals)
                        tp = round(fill_price - fill_price * TAKE_PROFIT_PCT, decimals)

                        open_trades[trade_id] = {"side": "sell", "entry": fill_price, "instrument": instrument}

                        log_trade_open(trade_id, instrument, "sell", reasons, fill_price, sl, tp)

                        msg = (
                            f"🔴 **SELL — {instrument}**\n"
                            f"Time: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}\n"
                            f"Trade ID: {trade_id}\n"
                            f"Entry Price: {fill_price}\n"
                            f"Stop Loss: {sl}\n"
                            f"Take Profit: {tp}\n"
                            f"Units: {TRADE_UNITS}\n"
                            f"RSI: {last['rsi']:.2f}\n"
                            f"Signal Strength: {strength_label(score_sell)}\n"
                            f"Confirmed by: {', '.join(reasons)}"
                        )
                        send_discord(msg)

                    else:
                        print("SIGNAL: No trade")

                    time.sleep(1)

                except Exception as e:
                    print(f"Error processing {instrument}: {e}")
                    continue

        except Exception as e:
            print(f"Error: {e}")
            send_discord(f"⚠️ Bot error: {e}")

        time.sleep(60 * 15)

run_bot()

