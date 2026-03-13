#!/usr/bin/env python3
"""
Aster Pilot Fund - DCA Trading Bot V3 (Strategy 2 - Production v4)
Fixes applied this version:
- Startup warmup: first loop observes price state only, no trades opened
- Pre-entry validation: never open a layer if current price already past stop loss
- All previous fixes retained
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

# ── Risk parameters ────────────────────────────────────────────────────
STOP_LOSS_PCT        = 3.0    # % loss before stop-loss triggers per layer
SL_COOLDOWN_MIN      = 60     # minutes to wait after a stop-loss
TRIGGER_STABILITY    = 0.1    # % max shift in 24h high/low between loops
LOOP_SLEEP_SECONDS   = 3      # check every 3 seconds
DAILY_DRAWDOWN_LIMIT = 5.0    # % max daily drawdown before bot stops trading
API_MIN_INTERVAL     = 3.0    # minimum seconds between Aster DEX API calls

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

# ── Prices ─────────────────────────────────────────────────────────────
def get_prices():
    try:
        resp    = api_get("https://fapi.asterdex.com/fapi/v1/ticker/24hr")
        tickers = resp.json()
        prices  = {}
        for t in tickers:
            symbol = t['symbol']
            prices[symbol] = {
                'price':      float(t['lastPrice']),
                'high_24h':   float(t['highPrice']),
                'low_24h':    float(t['lowPrice']),
                'change_pct': float(t['priceChangePercent']),
                'volume':     float(t['volume'])
            }
        return prices
    except Exception as e:
        print(f"Price fetch error: {e}")
        return {}

def get_account_equity():
    # TODO: wire in real Aster DEX API key authentication for live trading
    return None

# ── Trigger stability ──────────────────────────────────────────────────
def trigger_is_stable(current_trigger, last_trigger):
    if last_trigger is None:
        return True
    shift_pct = abs(current_trigger - last_trigger) / last_trigger * 100
    return shift_pct <= TRIGGER_STABILITY

# ── Stop loss checks ───────────────────────────────────────────────────
def check_stop_loss_long(entry, current_price):
    loss_pct = (entry - current_price) / entry * 100
    if loss_pct >= STOP_LOSS_PCT:
        return True, entry * (1 - STOP_LOSS_PCT / 100)
    return False, None

def check_stop_loss_short(entry, current_price):
    loss_pct = (current_price - entry) / entry * 100
    if loss_pct >= STOP_LOSS_PCT:
        return True, entry * (1 + STOP_LOSS_PCT / 100)
    return False, None

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
    drawdown_pct = (start - current_equity) / start * 100
    if drawdown_pct >= DAILY_DRAWDOWN_LIMIT:
        daily_state['trading_halted'] = True
        save_json(BASE_DIR / "daily_state.json", daily_state)
        send_telegram(
            f"🚨 <b>DAILY DRAWDOWN LIMIT HIT</b>\n"
            f"Start: ${start:.2f} | Now: ${current_equity:.2f}\n"
            f"Drawdown: -{drawdown_pct:.1f}% | Trading halted until tomorrow."
        )
        return True
    return False

# ── PnL calculations ───────────────────────────────────────────────────
def calc_pnl_long(entry, exit_price, position_size):
    return (exit_price - entry) / entry * position_size

def calc_pnl_short(entry, exit_price, position_size):
    return (entry - exit_price) / entry * position_size

# ── Open layer ─────────────────────────────────────────────────────────
def open_layer(symbol, layer_num, entry_price, side, strategy):
    rules         = strategy['dca']
    position_size = rules.get('layer_size', 50)
    leverage      = rules.get('leverage', 50)
    profit_target = entry_price * 1.01 if side == 'LONG' else entry_price * 0.99
    return {
        'symbol':         symbol,
        'side':           side,
        'layer':          layer_num,
        'entry_price':    entry_price,
        'profit_target':  profit_target,
        'position_size':  position_size,
        'leverage':       leverage,
        'status':         'open',
        'opened_at':      datetime.now().isoformat(),
        'unrealized_pnl': 0.0
    }

# ── Unrealized PnL ─────────────────────────────────────────────────────
def update_unrealized_pnl(open_trades, prices):
    for trade in open_trades:
        symbol = trade['symbol']
        if symbol not in prices:
            continue
        cp = prices[symbol]['price']
        trade['unrealized_pnl'] = calc_pnl_long(trade['entry_price'], cp, trade['position_size']) \
                                   if trade['side'] == 'LONG' else \
                                   calc_pnl_short(trade['entry_price'], cp, trade['position_size'])

# ── Warmup: build price state without trading ──────────────────────────
def warmup(symbols, prices):
    """
    FIX: On first startup, observe current price state without opening any trades.
    This populates price_state.json so the NEXT loop can detect genuine crossings.
    Without this, every symbol looks like a fresh crossing on startup.
    """
    print("🔍 Warmup loop — observing price state, no trades opened")
    state = {}
    for symbol in symbols:
        if symbol not in prices:
            continue
        high_24h = prices[symbol]['high_24h']
        low_24h  = prices[symbol]['low_24h']
        price    = prices[symbol]['price']

        long_trigger  = high_24h * 0.99
        short_trigger = low_24h  * 1.01

        state[f"{symbol}_LONG"] = {
            "was_above_trigger": price > long_trigger,
            "last_trigger":      long_trigger
        }
        state[f"{symbol}_SHORT"] = {
            "was_above_trigger": price < short_trigger,
            "last_trigger":      short_trigger
        }
        print(
            f"  {symbol}: price=${price:.4f} | "
            f"LONG trigger=${long_trigger:.4f} ({'above' if price > long_trigger else 'below'}) | "
            f"SHORT trigger=${short_trigger:.4f} ({'below' if price < short_trigger else 'above'})"
        )

    save_price_state(state)
    send_telegram("🔍 <b>Warmup complete</b> — price state observed, trading starts next loop")
    return state

# ── Process one side ───────────────────────────────────────────────────
def process_side(
    symbol, side, current_price, high_24h, low_24h,
    open_trades, trades_data, strategy,
    sl_cooldowns, prev_price_state, new_price_state
):
    modified   = False
    max_layers = strategy['dca']['max_layers']
    state_key  = f"{symbol}_{side}"
    prev_state = prev_price_state.get(state_key, {})

    if side == 'LONG':
        l1_trigger   = high_24h * 0.99
        is_above_now = current_price > l1_trigger
    else:
        l1_trigger   = low_24h * 1.01
        is_above_now = current_price < l1_trigger

    was_above    = prev_state.get("was_above_trigger", is_above_now)  # default to current, not True
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
            pnl = calc_pnl_long(trade['entry_price'], current_price, trade['position_size']) \
                  if side == 'LONG' else \
                  calc_pnl_short(trade['entry_price'], current_price, trade['position_size'])
            trade.update({
                'status':         'closed',
                'exit_price':     current_price,
                'closed_at':      datetime.now().isoformat(),
                'pnl':            round(pnl, 4),
                'unrealized_pnl': 0.0
            })
            trades_data['history'].append(trade)
            open_trades.remove(trade)
            layers.remove(trade)
            modified = True
            send_telegram(
                f"✅ <b>{side} L{trade['layer']} CLOSED</b>: {symbol}\n"
                f"Entry: ${trade['entry_price']:.4f} → Exit: ${current_price:.4f}\n"
                f"Profit: +${pnl:.2f}"
            )
            print(f"Closed {side} L{trade['layer']} {symbol} +${pnl:.2f}")
            continue

        hit_sl, sl_price = check_stop_loss_long(trade['entry_price'], current_price) \
                           if side == 'LONG' else \
                           check_stop_loss_short(trade['entry_price'], current_price)

        if hit_sl:
            pnl = calc_pnl_long(trade['entry_price'], sl_price, trade['position_size']) \
                  if side == 'LONG' else \
                  calc_pnl_short(trade['entry_price'], sl_price, trade['position_size'])
            trade.update({
                'status':         'stopped',
                'exit_price':     sl_price,
                'closed_at':      datetime.now().isoformat(),
                'pnl':            round(pnl, 4),
                'unrealized_pnl': 0.0
            })
            trades_data['history'].append(trade)
            open_trades.remove(trade)
            layers.remove(trade)
            modified = True
            sl_cooldowns[state_key] = datetime.now()
            save_cooldowns(sl_cooldowns)
            send_telegram(
                f"🛑 <b>STOP LOSS {side} L{trade['layer']}</b>: {symbol}\n"
                f"Entry: ${trade['entry_price']:.4f} → SL: ${sl_price:.4f}\n"
                f"Loss: ${pnl:.2f} (-{STOP_LOSS_PCT}%) | Cooldown: {SL_COOLDOWN_MIN}m"
            )
            print(f"Stop loss {side} L{trade['layer']} {symbol} ${pnl:.2f}")

    # ── STOP-LOSS COOLDOWN ───────────────────────────────────────────
    if state_key in sl_cooldowns:
        elapsed_min = (datetime.now() - sl_cooldowns[state_key]).total_seconds() / 60
        if elapsed_min < SL_COOLDOWN_MIN:
            print(f"{symbol} {side}: SL cooldown ({int(SL_COOLDOWN_MIN - elapsed_min)}m left)")
            return modified
        else:
            del sl_cooldowns[state_key]
            save_cooldowns(sl_cooldowns)

    # ── OPEN LAYER 1 ─────────────────────────────────────────────────
    if len(layers) == 0:
        fresh  = was_above and not is_above_now
        stable = trigger_is_stable(l1_trigger, last_trigger)

        if fresh and stable:
            # FIX: Pre-entry validation — never open if already past stop loss level
            sl_level = l1_trigger * (1 - STOP_LOSS_PCT / 100) if side == 'LONG' \
                       else l1_trigger * (1 + STOP_LOSS_PCT / 100)
            already_stopped = (side == 'LONG'  and current_price <= sl_level) or \
                              (side == 'SHORT' and current_price >= sl_level)

            if already_stopped:
                print(f"{symbol} {side}: Skipping L1 — price already past stop loss level at entry")
            else:
                new_layer = open_layer(symbol, 1, l1_trigger, side, strategy)
                open_trades.append(new_layer)
                layers.append(new_layer)
                modified = True
                ref = f"24h High: ${high_24h:.4f}" if side == 'LONG' else f"24h Low: ${low_24h:.4f}"
                send_telegram(
                    f"📥 <b>{side} L1 OPENED</b>: {symbol}\n"
                    f"Entry: ${l1_trigger:.4f}\n"
                    f"Target: ${l1_trigger * (1.01 if side == 'LONG' else 0.99):.4f} (+1%)\n"
                    f"{ref}"
                )
                print(f"Opened {side} L1 {symbol} @ ${l1_trigger:.4f}")

        elif fresh and not stable:
            shift = abs(l1_trigger - last_trigger) / last_trigger * 100 if last_trigger else 0
            print(f"{symbol} {side}: Trigger shifted {shift:.2f}% — skipping false entry")

    # ── OPEN LAYERS 2-10 ─────────────────────────────────────────────
    elif len(layers) < max_layers:
        last_layer     = max(layers, key=lambda x: x['layer'])
        next_layer_num = last_layer['layer'] + 1
        next_trigger   = last_layer['entry_price'] * (0.99 if side == 'LONG' else 1.01)

        existing_nums = [t['layer'] for t in layers]
        if next_layer_num in existing_nums:
            return modified

        layer_key        = f"{symbol}_{side}_L{next_layer_num}"
        prev_layer_state = prev_price_state.get(layer_key, {})
        was_above_layer  = prev_layer_state.get("was_above_trigger", True)
        last_layer_trig  = prev_layer_state.get("last_trigger", None)
        is_above_layer   = current_price > next_trigger if side == 'LONG' \
                           else current_price < next_trigger

        new_price_state[layer_key] = {
            "was_above_trigger": is_above_layer,
            "last_trigger":      next_trigger
        }

        fresh_layer  = was_above_layer and not is_above_layer
        stable_layer = trigger_is_stable(next_trigger, last_layer_trig)

        if fresh_layer and stable_layer:
            # FIX: Pre-entry validation for DCA layers too
            sl_level = next_trigger * (1 - STOP_LOSS_PCT / 100) if side == 'LONG' \
                       else next_trigger * (1 + STOP_LOSS_PCT / 100)
            already_stopped = (side == 'LONG'  and current_price <= sl_level) or \
                              (side == 'SHORT' and current_price >= sl_level)

            if already_stopped:
                print(f"{symbol} {side}: Skipping L{next_layer_num} — price already past stop loss level")
            else:
                new_layer = open_layer(symbol, next_layer_num, next_trigger, side, strategy)
                open_trades.append(new_layer)
                modified = True
                send_telegram(
                    f"📥 <b>{side} L{next_layer_num} OPENED</b>: {symbol}\n"
                    f"Entry: ${next_trigger:.4f}\n"
                    f"Target: ${next_trigger * (1.01 if side == 'LONG' else 0.99):.4f} (+1%)\n"
                    f"DCA {'below' if side == 'LONG' else 'above'} L{last_layer['layer']}"
                )
                print(f"Opened {side} L{next_layer_num} {symbol} @ ${next_trigger:.4f}")

    return modified


def main_loop():
    print("DCA Bot V3 (Strategy 2 - v4) started")

    sl_cooldowns     = load_cooldowns()
    prev_price_state = load_price_state()
    daily_state      = load_daily_state()

    # ── WARMUP: if no price state exists yet, observe first then trade ──
    is_warmup = len(prev_price_state) == 0

    while True:
        try:
            strategy    = load_strategy()
            trades_data = load_json(BASE_DIR / "trades.json", {"open": [], "history": []})

            if not strategy:
                print("No strategy loaded — retrying")
                time.sleep(5)
                continue

            if strategy.get('kill_switch'):
                print("Kill switch ON")
                time.sleep(60)
                continue

            prices = get_prices()
            if not prices:
                print("No price data — retrying")
                time.sleep(10)
                continue

            symbols     = strategy['dca']['symbols']
            open_trades = trades_data['open']

            # ── WARMUP LOOP — observe only, no trades ───────────────
            if is_warmup:
                prev_price_state = warmup(symbols, prices)
                is_warmup = False
                print("Warmup done — trading starts next loop")
                time.sleep(LOOP_SLEEP_SECONDS)
                continue

            update_unrealized_pnl(open_trades, prices)

            realized_pnl   = sum(t.get('pnl', 0) for t in trades_data['history'])
            unrealized_pnl = sum(t.get('unrealized_pnl', 0) for t in open_trades)
            base_equity    = get_account_equity() or 10000.0
            current_equity = base_equity + realized_pnl

            if daily_state.get('start_equity') is None:
                daily_state['start_equity'] = current_equity
                save_json(BASE_DIR / "daily_state.json", daily_state)

            if check_daily_drawdown(current_equity, daily_state):
                print(f"Trading halted — daily drawdown limit reached")
                time.sleep(60)
                continue

            if len(open_trades) >= 50:
                print("Max 50 layers reached globally")
                time.sleep(60)
                continue

            modified        = False
            new_price_state = dict(prev_price_state)

            for symbol in symbols:
                if symbol not in prices:
                    continue

                current_price = prices[symbol]['price']
                high_24h      = prices[symbol]['high_24h']
                low_24h       = prices[symbol]['low_24h']

                m = process_side(
                    symbol, 'LONG', current_price, high_24h, low_24h,
                    open_trades, trades_data, strategy,
                    sl_cooldowns, prev_price_state, new_price_state
                )
                modified = modified or m

                m = process_side(
                    symbol, 'SHORT', current_price, high_24h, low_24h,
                    open_trades, trades_data, strategy,
                    sl_cooldowns, prev_price_state, new_price_state
                )
                modified = modified or m

            prev_price_state = new_price_state
            save_price_state(new_price_state)

            long_open  = len([t for t in open_trades if t['side'] == 'LONG'])
            short_open = len([t for t in open_trades if t['side'] == 'SHORT'])

            save_json(BASE_DIR / "data.json", {
                'timestamp':      datetime.now().isoformat(),
                'equity':         round(current_equity, 2),
                'unrealized_pnl': round(unrealized_pnl, 2),
                'realized_pnl':   round(realized_pnl, 2),
                'total_pnl':      round(realized_pnl + unrealized_pnl, 2),
                'open_positions': len(open_trades),
                'long_layers':    long_open,
                'short_layers':   short_open,
                'daily_drawdown': round(
                    (daily_state['start_equity'] - current_equity) / daily_state['start_equity'] * 100, 2
                ) if daily_state.get('start_equity') else 0,
                'strategy':       'DCA Dual Long + Short - Strategy 2',
                'mode':           strategy.get('mode', 'paper'),
                'prices':         {s: prices[s]['price'] for s in symbols if s in prices}
            })

            if modified:
                save_json(BASE_DIR / "trades.json", trades_data)

            print(
                f"{datetime.now().strftime('%H:%M:%S')} | "
                f"Long: {long_open} | Short: {short_open} | "
                f"Equity: ${current_equity:.2f} | "
                f"Unrealized: ${unrealized_pnl:.2f} | "
                f"Realized: ${realized_pnl:.2f}"
            )
            time.sleep(LOOP_SLEEP_SECONDS)

        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(10)


if __name__ == "__main__":
    send_telegram(
        "🤖 <b>DCA Bot V3 started</b>\n"
        "Strategy 2: Dual DCA Long + Short\n"
        "• LONG: 1% below 24h high\n"
        "• SHORT: 1% above 24h low\n"
        "• Exit: +1% per layer independently\n"
        "• Stop loss: -3% with 1h cooldown\n"
        "• Daily drawdown limit: 5%\n"
        "• Warmup loop on fresh start\n"
        "• Pre-entry stop loss validation\n"
        "• Checking every 3 seconds"
    )
    main_loop()
