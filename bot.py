import os
import time
import numpy as np
import pandas as pd
import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Delta Exchange India tickers
COINS = {
    "BTC": "BTCUSD",
    "ETH": "ETHUSD",
    "SOL": "SOLUSD",
    "ZEC": "ZECUSD",
    "XAR": "XARUSD",
    "XAU": "PAXGUSD",
}

LAST_15M_TIME = {coin: None for coin in COINS}
LAST_5M_TIME = {coin: None for coin in COINS}
ACTIVE_TRADE = {coin: None for coin in COINS}
ENTRY_PRICES = {coin: 0.0 for coin in COINS}
TRAILING_SL_PRICES = {coin: 0.0 for coin in COINS}


def send_telegram_msg(text):
  if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    print("Missing Telegram credentials!")
    return
  url = f"https://telegram.org{TELEGRAM_BOT_TOKEN}/sendMessage"
  try:
    requests.post(
        url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10
    )
  except Exception as e:
    print(f"Telegram error: {e}")


def fetch_candles(symbol, resolution="15m", count=250):
  url = "https://delta.exchange"
  now = int(time.time())
  sec_per_candle = 900 if resolution == "15m" else 300
  start = now - (count * sec_per_candle)
  params = {
      "symbol": symbol,
      "resolution": resolution,
      "start": start,
      "end": now,
  }
  try:
    r = requests.get(url, params=params, timeout=10)
    if r.status_code != 200:
      print(f"HTTP {r.status_code} for {symbol}: {r.text[:120]}")
      return None
    data = r.json().get("result", [])
    if not data:
      return None
    df = pd.DataFrame(data)
    df = df.sort_values("time").reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
      df[col] = df[col].astype(float)
    return df
  except Exception as e:
    print(f"Error fetching {symbol} ({resolution}): {e}")
    return None


def calculate_parabolic_sar(df, af_start=0.02, af_step=0.02, af_max=0.20):
  high = df["high"].values
  low = df["low"].values
  n = len(df)
  sar = np.zeros(n)
  bull = True
  af = af_start
  ep = low[0]
  sar[0] = high[0]

  for i in range(1, n):
    if bull:
      sar[i] = sar[i - 1] + af * (ep - sar[i - 1])
      sar[i] = min(sar[i], low[i - 1], low[i - 2] if i >= 2 else low[i - 1])
      if low[i] < sar[i]:
        bull = False
        sar[i] = ep
        ep = low[i]
        af = af_start
      else:
        if high[i] > ep:
          ep = high[i]
          af = min(af + af_step, af_max)
    else:
      sar[i] = sar[i - 1] + af * (ep - sar[i - 1])
      sar[i] = max(sar[i], high[i - 1], high[i - 2] if i >= 2 else high[i - 1])
      if high[i] > sar[i]:
        bull = True
        sar[i] = ep
        ep = high[i]
        af = af_start
      else:
        if low[i] < ep:
          ep = low[i]
          af = min(af + af_step, af_max)
  return sar


def add_indicators(df):
  df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
  df["date"] = pd.to_datetime(df["time"], unit="s").dt.date
  df["tp"] = (df["high"] + df["low"] + df["close"]) / 3
  df["pv"] = df["tp"] * df["volume"]
  df["vwap"] = df.groupby("date")["pv"].cumsum() / df.groupby("date")[
      "volume"
  ].cumsum().replace(0, np.nan)
  df["vwap"] = df["vwap"].fillna(df["tp"])
  df["sar"] = calculate_parabolic_sar(df)

  delta = df["close"].diff()
  gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
  loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
  rs = gain / loss.replace(0, np.nan)
  rsi = 100 - (100 / (1 + rs))

  min_rsi = rsi.rolling(14).min()
  max_rsi = rsi.rolling(14).max()
  stoch_rsi = ((rsi - min_rsi) / (max_rsi - min_rsi).replace(0, np.nan)) * 100
  df["stoch_k"] = stoch_rsi.rolling(3).mean()
  df["stoch_d"] = df["stoch_k"].rolling(3).mean()
  return df


def scan_market(first_run=False):
  global LAST_15M_TIME, LAST_5M_TIME, ACTIVE_TRADE, ENTRY_PRICES, TRAILING_SL_PRICES
  active_coins = []

  for name, symbol in COINS.items():
    df_15m = fetch_candles(symbol, resolution="15m", count=250)
    if df_15m is None or len(df_15m) < 205:
      continue

    if first_run:
      active_coins.append(f"{name} ({df_15m['close'].iloc[-1]:.2f})")

    live_price = df_15m["close"].iloc[-1]

    # 1. ACTIVE TRADE MANAGEMENT (5m Trailing SL)
    if ACTIVE_TRADE[name] is not None:
      if ACTIVE_TRADE[name] == "BUY" and live_price <= TRAILING_SL_PRICES[name]:
        pnl_pct = (
            (TRAILING_SL_PRICES[name] - ENTRY_PRICES[name])
            / ENTRY_PRICES[name]
        ) * 100
        msg = (
            f"🛑 TRAILING SL HIT (BUY): {name}\nClosed at:"
            f" {TRAILING_SL_PRICES[name]:.4f} ({pnl_pct:+.2f}%)"
        )
        send_telegram_msg(msg)
        print(msg)
        ACTIVE_TRADE[name] = None
        continue

      elif (
          ACTIVE_TRADE[name] == "SELL"
          and live_price >= TRAILING_SL_PRICES[name]
      ):
        pnl_pct = (
            (ENTRY_PRICES[name] - TRAILING_SL_PRICES[name])
            / ENTRY_PRICES[name]
        ) * 100
        msg = (
            f"🛑 TRAILING SL HIT (SELL): {name}\nClosed at:"
            f" {TRAILING_SL_PRICES[name]:.4f} ({pnl_pct:+.2f}%)"
        )
        send_telegram_msg(msg)
        print(msg)
        ACTIVE_TRADE[name] = None
        continue

      df_5m = fetch_candles(symbol, resolution="5m", count=60)
      if df_5m is not None and len(df_5m) >= 20:
        df_5m["sar"] = calculate_parabolic_sar(df_5m)
        curr_5m = df_5m.iloc[-2]
        if LAST_5M_TIME[name] != curr_5m["time"]:
          LAST_5M_TIME[name] = curr_5m["time"]
          sar_5m = curr_5m["sar"]

          if ACTIVE_TRADE[name] == "BUY" and sar_5m < curr_5m["close"]:
            new_sl = sar_5m * 0.9985
            if new_sl > TRAILING_SL_PRICES[name]:
              TRAILING_SL_PRICES[name] = new_sl
              send_telegram_msg(
                  f"📈 5m TRAILING SL UPDATED (BUY): {name}\nNew SL:"
                  f" {new_sl:.4f}"
              )

          elif ACTIVE_TRADE[name] == "SELL" and sar_5m > curr_5m["close"]:
            new_sl = sar_5m * 1.0015
            if new_sl < TRAILING_SL_PRICES[name]:
              TRAILING_SL_PRICES[name] = new_sl
              send_telegram_msg(
                  f"📉 5m TRAILING SL UPDATED (SELL): {name}\nNew SL:"
                  f" {new_sl:.4f}"
              )

    # 2. NEW 15m ENTRY SCAN
    df_15m = add_indicators(df_15m)
    curr = df_15m.iloc[-2]
    prev = df_15m.iloc[-3]

    if LAST_15M_TIME[name] == curr["time"]:
      continue
    LAST_15M_TIME[name] = curr["time"]

    if ACTIVE_TRADE[name] is None:
      price = curr["close"]
      ema200 = curr["ema200"]
      vwap = curr["vwap"]
      sar_15m = curr["sar"]

      stoch_sell_hook = (
          max(prev["stoch_k"], curr["stoch_k"]) >= 80
          and prev["stoch_k"] >= prev["stoch_d"]
          and curr["stoch_k"] < curr["stoch_d"]
      )
      stoch_buy_hook = (
          min(prev["stoch_k"], curr["stoch_k"]) <= 20
          and prev["stoch_k"] <= prev["stoch_d"]
          and curr["stoch_k"] > curr["stoch_d"]
      )

      signal = None
      if price > ema200 and sar_15m < price and stoch_buy_hook:
        signal = "BUY"
        initial_sl = sar_15m * 0.9985
      elif price < ema200 and sar_15m > price and stoch_sell_hook:
        signal = "SELL"
        initial_sl = sar_15m * 1.0015

      if signal:
        ACTIVE_TRADE[name] = signal
        ENTRY_PRICES[name] = price
        TRAILING_SL_PRICES[name] = initial_sl
        risk_pct = (abs(price - initial_sl) / price) * 100
        emoji = "🟢" if signal == "BUY" else "🔴"
        vwap_status = "Above VWAP" if price > vwap else "Below VWAP"
        msg = (
            f"{emoji} {signal} ENTRY: {name} (15m)\n"
            f"Entry: {price:.4f}\n"
            f"🛡️ Initial SL (15m SAR): {initial_sl:.4f} (-{risk_pct:.2f}%)\n"
            f"EMA 200: {ema200:.4f} | VWAP: {vwap:.4f} ({vwap_status})\n"
            f"StochRSI K: {curr['stoch_k']:.1f}"
        )
        send_telegram_msg(msg)
        print(msg)

  if first_run:
    if active_coins:
      send_telegram_msg(
          "✅ Scanner Connected! Tracking:\n" + ", ".join(active_coins)
      )
    else:
      send_telegram_msg(
          "⚠️ Scanner started, but Delta Exchange blocked candle fetch (check"
          " GitHub Actions log)."
      )


def main():
  print("Starting GitHub Actions 15m/5m Scanner...")
  for cycle in range(55):
    scan_market(first_run=(cycle == 0))
    if cycle == 0:
      print("Initial scan completed successfully.")
    time.sleep(60)


if __name__ == "__main__":
  main()
