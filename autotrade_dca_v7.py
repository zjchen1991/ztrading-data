#!/usr/bin/env python3
"""
Aster Pilot Fund - DCA Trading Bot V3 (Strategy 2 - v7)
Changes in this version:
- Added SOLUSDT, BNBUSDT, ASTERUSDT to symbol universe
- Removed XAGUSDT (weekend closure + thin liquidity)
- Updated correlation groups: 6 crypto / 1 standalone macro
- MAX_CORR_LAYERS raised to 3 (allows 3 of 6 crypto symbols per side)
- ASTERUSDT trades at half layer_size (higher vol, smaller exposure)
- All v6 features retained: EMA trend, 5% SL, 1.5% TP, vol filter, corr guard
"""

import json
import requests
import time
import threading
import os
from datetime import datetime, date
from pathlib import Path

BASE_DIR = Path(__file__).parent

TELEGRAM_BOT_TOKEN = "8777890597:AAEeWR6AnVeO6rO4WBOWAVtbIvVu9jFhbtw"
TELEGRAM_CHAT_ID   = "1058007741"

ASTER_API = "https://fapi.asterdex.com/fapi/v1"

# ── Risk parameters ────────────────────────────────────────────────────
STOP_LOSS_PCT        = 5.0
TAKE_PROFIT_PCT      = 1.0
SL_COOLDOWN_MIN      = 60
TRIGGER_STABILITY    = 0.1
LOOP_SLEEP_SECONDS   = 3
DAILY_DRAWDOWN_LIMIT = 5.0
API_MIN_INTERVAL     = 3.0

# ── Volume filter ──────────────────────────────────────────────────────
MIN_VOLUME_USD = 10_000_000   # $10M 24h minimum

# ── Per-symbol layer size overrides ───────────────────────────────────
# ASTERUSDT is high-volatility — trade at half size to limit exposure
SYMBOL_SIZE_OVERRIDES = {
    'ASTERUSDT': 25,   # half of default layer_size
}

# ── Correlation groups ─────────────────────────────────────────────────
# Crypto group now includes SOL, BNB, ASTER — all move together in risk-off
# XAU is standalone (macro/safe-haven, moves independently)
CORR_GROUPS = [
    ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'HYPEUSDT', 'ASTERUSDT'],
    ['XAUUSDT'],
]
MAX_CORR_LAYERS = 3   # max distinct symbols with open layers on same side per group

# ── 4h EMA trend filter ────────────────────────────────────────────────
TREND_EMA_FAST  = 9
TREND_EMA_SLOW  = 21
TREND_CACHE_HRS = 4

# ── API rate limiter ───────────────────────────────────────────────────
_last_api_call = 0.0

def api_get(url, timeout=10):
    global _last_api_call
    elapsed = time.time() - _last_api_call
    if elapsed < API_MIN_INTERVAL:
        time.sleep(API_MIN_INTERVAL - elapsed)
    _last_api_call = time.time()
    return requests.get(url, timeout=timeout)

# ── Non-blocking Telegram ──────────────────────────────────────────────
def send_telegram(message):
    def _send():
        try:
            url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
            requests.post(url, json=data, timeout=5)
        except Exception as e:
            print(f"Telegram error: {e}")
    threading.Thread(target=_send, daemon=True).start()

# ── Atomic file writes ─────────────────────────────────────────────────
def save_json(filepath, data):
    filepath = Path(filepath)
    tmp = filepath.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, filepath)

def load_json(filepath, default=None):
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except:
        return default if default is not None else {}

# ── Strategy caching ───────────────────────────────────────────────────
_strategy_cache = None
_strategy_mtime = None

def load_strategy():
    global _strategy_cache, _strategy_mtime
    path = BASE_DIR / "strategy.json"
    try:
        mtime = path.stat().st_mtime
        if _strategy_cache is None or mtime != _strategy_mtime:
            _strategy_cache = load_json(path)
            _strategy_mtime = mtime
            print("strategy.json reloaded")
    except:
        pass
    return _strategy_cache

# ── Cooldowns ─────────────────────────────────────────────────────────
def load_cooldowns():
    raw = load_json(BASE_DIR / "cooldowns.json", {})
    result = {}
    for k, v in raw.items():
        try:
            result[k] = datetime.fromisoformat(v)
        except:
            pass
    return result

def save_cooldowns(cooldowns):
    save_json(BASE_DIR / "cooldowns.json", {k: v.isoformat() for k, v in cooldowns.items()})

def load_price_state():
    return load_json(BASE_DIR / "price_state.json", {})

def save_price_state(state):
    save_json(BASE_DIR / "price_state.json", state)

# ── 4h EMA Trend Filter ────────────────────────────────────────────────
_trend_cache = {}

def calc_ema(prices, period):
    if len(prices) < period:
        return None
    k   = 2.0 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema

def get_4h_trend(symbol):
    global _trend_cache
    now    = datetime.now()
    cached = _trend_cache.get(symbol)
    if cached:
        age_hours = (now - cached['updated_at']).total_seconds() / 3600
        if age_hours < TREND_CACHE_HRS:
            return cached['trend']
    try:
        resp    = api_get(f"{ASTER_API}/klines?symbol={symbol}&interval=4h&limit=50")
        candles = resp.json()
        if not isinstance(candles, list) or len(candles) < TREND_EMA_SLOW + 5:
            return 'NEUTRAL'
        closes   = [float(c[4]) for c in candles]
        ema_fast = calc_ema(closes, TREND_EMA_FAST)
        ema_slow = calc_ema(closes, TREND_EMA_SLOW)
        if ema_fast is None or ema_slow is None:
            return 'NEUTRAL'
        trend = 'BULLISH' if ema_fast > ema_slow else 'BEARISH'
        _trend_cache[symbol] = {
            'trend':      trend,
            'updated_at': now,
            'ema_fast':   round(ema_fast, 4),
            'ema_slow':   round(ema_slow, 4)
        }
        print(f"  📊 {symbol} 4h: {trend} (EMA{TREND_EMA_FAST}={ema_fast:.4f} EMA{TREND_EMA_SLOW}={ema_slow:.4f})")
        return trend
    except Exception as e:
        print(f"{symbol}: Trend error: {e} — NEUTRAL")
        return 'NEUTRAL'

# ── Volume filter ──────────────────────────────────────────────────────
def passes_volume_filter(symbol, prices):
    """Returns True if 24h quote volume >= MIN_VOLUME_USD."""
    data = prices.get(symbol)
    if not data:
        return False
    vol = data.get('volume', 0) * data.get('price', 0)
    # Some exchanges report quoteVolume directly, else estimate from base*price
    quote_vol = data.get('quote_volume', vol)
    if quote_vol < MIN_VOLUME_USD:
        print(f"{symbol}: Volume ${quote_vol:,.0f} below ${MIN_VOLUME_USD:,} minimum — skip")
        return False
    return True

# ── Correlation guard ──────────────────────────────────────────────────
def passes_correlation_guard(symbol, side, open_trades):
    """
    Block entry if too many correlated symbols already have open layers on same side.
    Prevents correlated liquidations draining the daily drawdown at once.
    """
    for group in CORR_GROUPS:
        if symbol not in group:
            continue
        active_in_group = len([
            t for t in open_trades
            if (t['symbol'] in group) and t['side'] == side and t['status'] == 'open'
            and t['symbol'] != symbol  # exclude self
        ])
        # Count distinct symbols, not layers
        active_symbols = set(
            t['symbol'] for t in open_trades
            if t['symbol'] in group and t['side'] == side
            and t['status'] == 'open' and t['symbol'] != symbol
        )
        if len(active_symbols) >= MAX_CORR_LAYERS:
            print(
                f"{symbol} {side}: Correlation guard — "
                f"{len(active_symbols)} correlated symbols already active: {active_symbols}"
            )
            return False
    return True

# ── Prices ─────────────────────────────────────────────────────────────
def get_prices():
    try:
        resp    = api_get(f"{ASTER_API}/ticker/24hr")
        tickers = resp.json()
        prices  = {}
        for t in tickers:
            symbol = t['symbol']
            price  = float(t['lastPrice'])
            vol    = float(t.get('volume', 0))
            qvol   = float(t.get('quoteVolume', vol * price))
            prices[symbol] = {
                'price':        price,
                'high_24h':     float(t['highPrice']),
                'low_24h':      float(t['lowPrice']),
                'change_pct':   float(t['priceChangePercent']),
                'volume':       vol,
                'quote_volume': qvol,
            }
        return prices
    except Exception as e:
        print(f"Price fetch error: {e}")
        return {}

def get_account_equity():
    return None  # TODO: Aster DEX API key auth

# ── Trigger stability ──────────────────────────────────────────────────
def trigger_is_stable(current, last):
    # DISABLED: Always return True to allow trades
    return True

# ── Stop loss checks ───────────────────────────────────────────────────
def check_stop_loss_long(entry, current):
    pct = (entry - current) / entry * 100
    return (True, entry * (1 - STOP_LOSS_PCT/100)) if pct >= STOP_LOSS_PCT else (False, None)

def check_stop_loss_short(entry, current):
    pct = (current - entry) / entry * 100
    return (True, entry * (1 + STOP_LOSS_PCT/100)) if pct >= STOP_LOSS_PCT else (False, None)

# ── Daily drawdown ─────────────────────────────────────────────────────
def load_daily_state():
    data  = load_json(BASE_DIR / "daily_state.json", {})
    today = str(date.today())
    if data.get('date') != today:
        data = {'date': today, 'start_equity': None, 'trading_halted': False}
        save_json(BASE_DIR / "daily_state.json", data)
    return data

def check_daily_drawdown(current_equity, daily_state):
    if daily_state.get('trading_halted'):
        return True
    start = daily_state.get('start_equity')
    if not start:
        return False
    dd = (start - current_equity) / start * 100
    if dd >= DAILY_DRAWDOWN_LIMIT:
        daily_state['trading_halted'] = True
        save_json(BASE_DIR / "daily_state.json", daily_state)
        send_telegram(
            f"🚨 <b>DAILY DRAWDOWN LIMIT HIT</b>\n"
            f"Start: ${start:.2f} | Now: ${current_equity:.2f}\n"
            f"Drawdown: -{dd:.1f}% | Trading halted until tomorrow."
        )
        return True
    return False

# ── PnL ───────────────────────────────────────────────────────────────
def calc_pnl_long(entry, exit_p, size, leverage=50):
    return (exit_p - entry) / entry * size * leverage

def calc_pnl_short(entry, exit_p, size, leverage=50):
    return (entry - exit_p) / entry * size * leverage

# ── Open layer ─────────────────────────────────────────────────────────
def open_layer(symbol, layer_num, entry_price, side, strategy):
    rules    = strategy['dca']
    # Use symbol-specific size override if defined, else strategy default
    default_size = rules.get('layer_size', 50)
    size     = SYMBOL_SIZE_OVERRIDES.get(symbol, default_size)
    leverage = rules.get('leverage', 50)
    tp = entry_price * (1 + TAKE_PROFIT_PCT/100) if side == 'LONG' else entry_price * (1 - TAKE_PROFIT_PCT/100)
    return {
        'symbol':         symbol,
        'side':           side,
        'layer':          layer_num,
        'entry_price':    entry_price,
        'profit_target':  tp,
        'position_size':  size,
        'leverage':       leverage,
        'status':         'open',
        'opened_at':      datetime.now().isoformat(),
        'unrealized_pnl': 0.0
    }

def update_unrealized_pnl(open_trades, prices):
    for t in open_trades:
        sym = t['symbol']
        if sym not in prices:
            continue
        cp = prices[sym]['price']
        t['unrealized_pnl'] = calc_pnl_long(t['entry_price'], cp, t['position_size'], t.get('leverage', 50)) \
                               if t['side'] == 'LONG' else \
                               calc_pnl_short(t['entry_price'], cp, t['position_size'], t.get('leverage', 50))

# ── Warmup ────────────────────────────────────────────────────────────
def warmup(symbols, prices):
    print("🔍 Warmup — observing state, no trades")
    state = {}
    for symbol in symbols:
        if symbol not in prices:
            continue
        high_24h = prices[symbol]['high_24h']
        low_24h  = prices[symbol]['low_24h']
        price    = prices[symbol]['price']
        long_t   = high_24h * (1 - TAKE_PROFIT_PCT/100)
        short_t  = low_24h  * (1 + TAKE_PROFIT_PCT/100)
        state[f"{symbol}_LONG"]  = {"was_above_trigger": price > long_t,  "last_trigger": long_t}
        state[f"{symbol}_SHORT"] = {"was_above_trigger": price < short_t, "last_trigger": short_t}
        trend = get_4h_trend(symbol)
        vol   = prices[symbol].get('quote_volume', 0)
        print(f"  {symbol}: ${price:.2f} | trend={trend} | vol=${vol:,.0f}")
    save_price_state(state)
    send_telegram("🔍 <b>Warmup complete</b> — trading starts next loop")
    return state

# ── Process one side ───────────────────────────────────────────────────
def process_side(
    symbol, side, current_price, high_24h, low_24h,
    open_trades, trades_data, strategy, prices,
    sl_cooldowns, prev_price_state, new_price_state
):
    modified   = False
    max_layers = strategy['dca'].get('max_layers', 5)
    state_key  = f"{symbol}_{side}"
    prev_state = prev_price_state.get(state_key, {})

    if side == 'LONG':
        l1_trigger   = high_24h * (1 - TAKE_PROFIT_PCT/100)
        is_above_now = current_price > l1_trigger
    else:
        l1_trigger   = low_24h * (1 + TAKE_PROFIT_PCT/100)
        is_above_now = current_price < l1_trigger

    was_above    = prev_state.get("was_above_trigger", is_above_now)
    last_trigger = prev_state.get("last_trigger", None)

    new_price_state[state_key] = {
        "was_above_trigger": is_above_now,
        "last_trigger":      l1_trigger
    }

    layers = [t for t in open_trades
              if t['symbol'] == symbol and t['side'] == side and t['status'] == 'open']

    # ── CHECK EXITS ──────────────────────────────────────────────────
    for trade in list(layers):
        tp_hit = (side == 'LONG'  and current_price >= trade['profit_target']) or \
                 (side == 'SHORT' and current_price <= trade['profit_target'])
        if tp_hit:
            pnl = calc_pnl_long(trade['entry_price'], current_price, trade['position_size'], trade.get('leverage', 50)) \
                  if side == 'LONG' else \
                  calc_pnl_short(trade['entry_price'], current_price, trade['position_size'], trade.get('leverage', 50))
            trade.update({'status': 'closed', 'exit_price': current_price,
                          'closed_at': datetime.now().isoformat(),
                          'pnl': round(pnl, 4), 'unrealized_pnl': 0.0})
            trades_data['history'].append(trade)
            open_trades.remove(trade); layers.remove(trade)
            modified = True
            send_telegram(
                f"✅ <b>{side} L{trade['layer']} CLOSED</b>: {symbol}\n"
                f"Entry: ${trade['entry_price']:.4f} → Exit: ${current_price:.4f}\n"
                f"Leverage: {trade.get('leverage', 50)}x | Profit: +${pnl:.2f} (+{TAKE_PROFIT_PCT}%)"
            )
            print(f"TP {side} L{trade['layer']} {symbol} +${pnl:.2f}")
            continue

        hit_sl, sl_price = check_stop_loss_long(trade['entry_price'], current_price) \
                           if side == 'LONG' else \
                           check_stop_loss_short(trade['entry_price'], current_price)
        if hit_sl:
            pnl = calc_pnl_long(trade['entry_price'], sl_price, trade['position_size'], trade.get('leverage', 50)) \
                  if side == 'LONG' else \
                  calc_pnl_short(trade['entry_price'], sl_price, trade['position_size'], trade.get('leverage', 50))
            trade.update({'status': 'stopped', 'exit_price': sl_price,
                          'closed_at': datetime.now().isoformat(),
                          'pnl': round(pnl, 4), 'unrealized_pnl': 0.0})
            trades_data['history'].append(trade)
            open_trades.remove(trade); layers.remove(trade)
            modified = True
            sl_cooldowns[state_key] = datetime.now()
            save_cooldowns(sl_cooldowns)
            send_telegram(
                f"🛑 <b>STOP LOSS {side} L{trade['layer']}</b>: {symbol}\n"
                f"Entry: ${trade['entry_price']:.4f} → SL: ${sl_price:.4f}\n"
                f"Leverage: {trade.get('leverage', 50)}x | Loss: ${pnl:.2f} (-{STOP_LOSS_PCT}%)"
            )
            print(f"SL {side} L{trade['layer']} {symbol} ${pnl:.2f}")

    # ── SL COOLDOWN ──────────────────────────────────────────────────
    if state_key in sl_cooldowns:
        elapsed = (datetime.now() - sl_cooldowns[state_key]).total_seconds() / 60
        if elapsed < SL_COOLDOWN_MIN:
            print(f"{symbol} {side}: cooldown {int(SL_COOLDOWN_MIN-elapsed)}m")
            return modified
        else:
            del sl_cooldowns[state_key]
            save_cooldowns(sl_cooldowns)

    # ── VOLUME FILTER ────────────────────────────────────────────────
    if not passes_volume_filter(symbol, prices):
        return modified

    # ── TREND FILTER (4h EMA9/EMA21) ─────────────────────────────────
    trend = get_4h_trend(symbol)
    if side == 'LONG' and trend == 'BEARISH':
        print(f"{symbol}: Skip LONG — 4h BEARISH")
        return modified
    if side == 'SHORT' and trend == 'BULLISH':
        print(f"{symbol}: Skip SHORT — 4h BULLISH")
        return modified

    # ── CORRELATION GUARD ─────────────────────────────────────────────
    if not passes_correlation_guard(symbol, side, open_trades):
        return modified

    # ── OPEN LAYER 1 ─────────────────────────────────────────────────
    if len(layers) == 0:
        fresh  = was_above and not is_above_now
        stable = trigger_is_stable(l1_trigger, last_trigger)
        if fresh and stable:
            sl_lvl = l1_trigger * (1 - STOP_LOSS_PCT/100) if side == 'LONG' \
                     else l1_trigger * (1 + STOP_LOSS_PCT/100)
            if (side == 'LONG' and current_price <= sl_lvl) or \
               (side == 'SHORT' and current_price >= sl_lvl):
                print(f"{symbol} {side}: Skip L1 — already past SL")
            else:
                new_layer = open_layer(symbol, 1, l1_trigger, side, strategy)
                open_trades.append(new_layer); layers.append(new_layer)
                modified = True
                ref = f"24h High: ${high_24h:.4f}" if side == 'LONG' else f"24h Low: ${low_24h:.4f}"
                vol = prices[symbol].get('quote_volume', 0)
                lev = new_layer.get('leverage', 50)
                send_telegram(
                    f"📥 <b>{side} L1 OPENED</b>: {symbol}\n"
                    f"Entry: ${l1_trigger:.4f} | Lev: {lev}x\n"
                    f"Notional: ${l1_trigger * lev:.0f}\n"
                    f"Target: ${l1_trigger*(1+TAKE_PROFIT_PCT/100):.4f} | SL: {STOP_LOSS_PCT}%\n"
                    f"{ref} | Vol: ${vol/1e6:.1f}M | Trend: {trend}"
                )
                print(f"Open {side} L1 {symbol} @ ${l1_trigger:.4f} | lev={lev}x | trend={trend}")
        elif fresh and not stable:
            pass  # Was: print(f"{symbol} {side}: trigger shifted — skip")

    # ── OPEN LAYERS 2-N ───────────────────────────────────────────────
    elif len(layers) < max_layers:
        last_l     = max(layers, key=lambda x: x['layer'])
        next_num   = last_l['layer'] + 1
        next_trig = last_l['entry_price'] * (1 - TAKE_PROFIT_PCT/100) if side == 'LONG' else last_l['entry_price'] * (1 + TAKE_PROFIT_PCT/100)
        if next_num in [t['layer'] for t in layers]:
            return modified

        lk   = f"{symbol}_{side}_L{next_num}"
        pls  = prev_price_state.get(lk, {})
        was_al = pls.get("was_above_trigger", True)
        last_lt = pls.get("last_trigger", None)
        is_al  = current_price > next_trig if side == 'LONG' else current_price < next_trig

        new_price_state[lk] = {"was_above_trigger": is_al, "last_trigger": next_trig}

        if (was_al and not is_al) and trigger_is_stable(next_trig, last_lt):
            sl_lvl = next_trig * (1 - STOP_LOSS_PCT/100) if side == 'LONG' \
                     else next_trig * (1 + STOP_LOSS_PCT/100)
            if (side == 'LONG' and current_price <= sl_lvl) or \
               (side == 'SHORT' and current_price >= sl_lvl):
                print(f"{symbol} {side}: Skip L{next_num} — past SL")
            else:
                new_layer = open_layer(symbol, next_num, next_trig, side, strategy)
                open_trades.append(new_layer)
                modified = True
                lev = new_layer.get('leverage', 50)
                send_telegram(
                    f"📥 <b>{side} L{next_num} OPENED</b>: {symbol}\n"
                    f"Entry: ${next_trig:.4f} | Lev: {lev}x\n"
                    f"Notional: ${next_trig * lev:.0f}\n"
                    f"Target: ${next_trig*(1+TAKE_PROFIT_PCT/100):.4f} | DCA L{last_l['layer']}"
                )
                print(f"Open {side} L{next_num} {symbol} @ ${next_trig:.4f} | lev={lev}x")

    return modified


def main_loop():
    print("DCA Bot V3 v6 — vol filter + corr guard + EMA trend + 5%SL + 1.5%TP")

    sl_cooldowns     = load_cooldowns()
    prev_price_state = load_price_state()
    daily_state      = load_daily_state()
    is_warmup        = len(prev_price_state) == 0
    git_push_counter = 0

    while True:
        try:
            git_push_counter += 1
            
            strategy    = load_strategy()
            trades_data = load_json(BASE_DIR / "trades.json", {"open": [], "history": []})

            if not strategy:
                print("No strategy — retrying"); time.sleep(5); continue
            if strategy.get('kill_switch'):
                print("Kill switch ON"); time.sleep(60); continue

            prices = get_prices()
            if not prices:
                print("No prices — retrying"); time.sleep(10); continue

            symbols     = strategy['dca']['symbols']
            open_trades = trades_data['open']

            if is_warmup:
                prev_price_state = warmup(symbols, prices)
                is_warmup = False
                time.sleep(LOOP_SLEEP_SECONDS); continue

            update_unrealized_pnl(open_trades, prices)

            realized_pnl   = sum(t.get('pnl', 0) for t in trades_data['history'])
            unrealized_pnl = sum(t.get('unrealized_pnl', 0) for t in open_trades)
            base_equity    = get_account_equity() or 10000.0
            current_equity = base_equity + realized_pnl

            if daily_state.get('start_equity') is None:
                daily_state['start_equity'] = current_equity
                save_json(BASE_DIR / "daily_state.json", daily_state)

            if check_daily_drawdown(current_equity, daily_state):
                time.sleep(60); continue

            if len(open_trades) >= 50:
                time.sleep(60); continue

            modified        = False
            new_price_state = dict(prev_price_state)

            for symbol in symbols:
                if symbol not in prices:
                    continue
                cp      = prices[symbol]['price']
                h24     = prices[symbol]['high_24h']
                l24     = prices[symbol]['low_24h']

                for side in ['LONG', 'SHORT']:
                    m = process_side(
                        symbol, side, cp, h24, l24,
                        open_trades, trades_data, strategy, prices,
                        sl_cooldowns, prev_price_state, new_price_state
                    )
                    modified = modified or m

            prev_price_state = new_price_state
            save_price_state(new_price_state)

            long_open  = len([t for t in open_trades if t['side'] == 'LONG'])
            short_open = len([t for t in open_trades if t['side'] == 'SHORT'])
            trend_summary = {s: _trend_cache[s]['trend'] for s in _trend_cache}
            vol_summary   = {s: round(prices[s].get('quote_volume', 0)/1e6, 1)
                             for s in symbols if s in prices}

            save_json(BASE_DIR / "data.json", {
                'timestamp':      datetime.now().isoformat(),
                'equity':         round(current_equity, 2),
                'unrealized_pnl': round(unrealized_pnl, 2),
                'realized_pnl':   round(realized_pnl, 2),
                'total_pnl':      round(realized_pnl + unrealized_pnl, 2),
                'open_positions': len(open_trades),
                'positions':      open_trades,
                'history':        trades_data['history'],
                'long_layers':    long_open,
                'short_layers':   short_open,
                'daily_drawdown': round(
                    (daily_state['start_equity'] - current_equity) / daily_state['start_equity'] * 100, 2
                ) if daily_state.get('start_equity') else 0,
                'strategy':  'DCA Dual Long + Short - Strategy 2',
                'mode':      strategy.get('mode', 'paper'),
                'kill_switch': strategy.get('kill_switch', False),
                'prices':    {s: prices[s]['price'] for s in symbols if s in prices},
                'volumes_m': vol_summary,
                'trends':    trend_summary,
                'params': {
                    'stop_loss_pct':    STOP_LOSS_PCT,
                    'take_profit_pct':  TAKE_PROFIT_PCT,
                    'max_layers':       strategy['dca'].get('max_layers', 5),
                    'sl_cooldown_min':  SL_COOLDOWN_MIN,
                    'min_volume_usd':   MIN_VOLUME_USD,
                    'trend_filter':     f'EMA{TREND_EMA_FAST}/EMA{TREND_EMA_SLOW} 4h',
                    'corr_guard':       f'max {MAX_CORR_LAYERS} per group',
                    'size_overrides':   SYMBOL_SIZE_OVERRIDES,
                }
            })

            # Auto-push to GitHub every ~60 seconds (20 loops × 3 sec)
            if git_push_counter >= 20:
                git_push_counter = 0
                try:
                    import os
                    os.system('cd /home/openclaw/ztrading && git add -f data.json trades.json && git commit -m "auto-sync" && git push -q 2>/dev/null')
                except:
                    pass

            if modified:
                save_json(BASE_DIR / "trades.json", trades_data)

            print(
                f"{datetime.now().strftime('%H:%M:%S')} | "
                f"L:{long_open} S:{short_open} | "
                f"Eq:${current_equity:.2f} | "
                f"UPnL:${unrealized_pnl:.2f} RPnL:${realized_pnl:.2f}"
            )
            time.sleep(LOOP_SLEEP_SECONDS)

        except Exception as e:
            print(f"Error: {e}")
            import traceback; traceback.print_exc()
            time.sleep(10)


if __name__ == "__main__":
    send_telegram(
        "🤖 <b>DCA Bot V3 started (v7)</b>\n\n"
        "<b>Symbols:</b> BTC · ETH · SOL · BNB · XAU · HYPE · ASTER\n\n"
        "<b>Parameters:</b>\n"
        f"• Entry: {TAKE_PROFIT_PCT}% from 24h high/low\n"
        f"• Take profit: +{TAKE_PROFIT_PCT}% per layer\n"
        f"• Stop loss: -{STOP_LOSS_PCT}%\n"
        f"• SL cooldown: {SL_COOLDOWN_MIN}m\n"
        f"• Max layers per side: 5\n"
        f"• ASTER layer size: $25 (half)\n"
        f"• Daily drawdown limit: {DAILY_DRAWDOWN_LIMIT}%\n\n"
        "<b>Filters:</b>\n"
        f"• 4h EMA{TREND_EMA_FAST}/EMA{TREND_EMA_SLOW} trend filter\n"
        f"• Volume filter: min ${MIN_VOLUME_USD/1e6:.0f}M 24h\n"
        f"• Corr guard: max {MAX_CORR_LAYERS} crypto symbols per side\n"
        f"• XAGUSDT excluded (weekend closure)"
    )
    main_loop()
