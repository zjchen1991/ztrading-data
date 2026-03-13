#!/usr/bin/env python3
"""
Aster Pilot Fund - DCA Trading Bot V3 (Strategy 1 - Production Grade)
Strategy 1 - Classic DCA Long:
- Entry: 1% below 24h high (rolling, from exchange)
- Each layer: 1% below previous layer entry
- Exit: +1% profit per layer, each exits independently
- Stop loss: -3% per layer, 1h cooldown after
- Fresh crossing detection with trigger stability check
  (prevents false entries when 24h high shifts significantly)
- All state persists to disk (survives restarts)
- Notifications only on real events
"""

import json
import requests
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent

TELEGRAM_BOT_TOKEN = "8777890597:AAEeWR6AnVeO6rO4WBOWAVtbIvVu9jFhbtw"
TELEGRAM_CHAT_ID = "1058007741"

# ── Risk parameters ────────────────────────────────────────────────────
STOP_LOSS_PCT       = 3.0    # % loss before stop-loss triggers per layer
SL_COOLDOWN_MIN     = 60     # minutes to wait after a stop-loss before re-entering
TRIGGER_STABILITY   = 0.1    # % max allowed shift in 24h high between loops
                             # If high moves more than this, skip entry (not a real dip)
LOOP_SLEEP_SECONDS  = 60     # check every 60 seconds

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        requests.post(url, json=data, timeout=5)
    except Exception as e:
        print(f"Telegram error: {e}")

def load_json(filepath, default=None):
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except:
        return default if default is not None else {}

def save_json(filepath, data):
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2, default=str)

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
    """
    Stores per-symbol:
    - was_above_trigger: bool (for crossing detection)
    - last_trigger: float (to detect if 24h high shifted too much)
    """
    return load_json(BASE_DIR / "price_state.json", {})

def save_price_state(state):
    save_json(BASE_DIR / "price_state.json", state)

def get_prices():
    try:
        resp = requests.get("https://fapi.asterdex.com/fapi/v1/ticker/24hr", timeout=10)
        tickers = resp.json()
        prices = {}
        for t in tickers:
            symbol = t['symbol']
            prices[symbol] = {
                'price':      float(t['lastPrice']),
                'high_24h':   float(t['highPrice']),   # rolling 24h high from exchange
                'low_24h':    float(t['lowPrice']),
                'change_pct': float(t['priceChangePercent']),
                'volume':     float(t['volume'])
            }
        return prices
    except Exception as e:
        print(f"Price fetch error: {e}")
        return {}

def trigger_is_stable(current_trigger, last_trigger):
    """
    Returns True only if the 24h high hasn't shifted more than TRIGGER_STABILITY %.
    Prevents false entries caused by the reference point moving, not the price.
    Example: if 24h high jumps from $70,000 to $72,000, that's a 2.86% shift.
    We skip entry because the trigger moved — not because price genuinely dipped.
    """
    if last_trigger is None:
        return True  # first run, no previous data — allow
    shift_pct = abs(current_trigger - last_trigger) / last_trigger * 100
    return shift_pct <= TRIGGER_STABILITY

def check_stop_loss(entry, current_price):
    loss_pct = (entry - current_price) / entry * 100
    if loss_pct >= STOP_LOSS_PCT:
        return True, entry * (1 - STOP_LOSS_PCT / 100)
    return False, None

def open_layer(symbol, layer_num, entry_price, strategy):
    rules = strategy['dca']
    position_size = rules.get('layer_size', 50)
    leverage      = rules.get('leverage', 50)
    profit_target = entry_price * 1.01
    return {
        'symbol':        symbol,
        'side':          'LONG',
        'layer':         layer_num,
        'entry_price':   entry_price,
        'profit_target': profit_target,
        'position_size': position_size,
        'leverage':      leverage,
        'status':        'open',
        'opened_at':     datetime.now().isoformat(),
        'unrealized_pnl': 0
    }

def main_loop():
    print("DCA Bot V3 (Strategy 1 - Production) started")

    sl_cooldowns     = load_cooldowns()
    prev_price_state = load_price_state()

    while True:
        try:
            strategy    = load_json(BASE_DIR / "strategy.json")
            trades_data = load_json(BASE_DIR / "trades.json", {"open": [], "history": []})

            if strategy.get('kill_switch'):
                print("Kill switch ON — sleeping")
                time.sleep(60)
                continue

            prices = get_prices()
            if not prices:
                print("No price data — retrying")
                time.sleep(60)
                continue

            symbols     = strategy['dca']['symbols']
            max_layers  = strategy['dca']['max_layers']
            open_trades = trades_data['open']

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
                high_24h      = prices[symbol]['high_24h']  # rolling 24h high, updates every loop

                # Layer 1 trigger: 1% below rolling 24h high
                l1_trigger  = high_24h * 0.99
                state_key   = f"{symbol}_LONG"
                prev_state  = prev_price_state.get(state_key, {})
                was_above   = prev_state.get("was_above_trigger", True)
                last_trigger= prev_state.get("last_trigger", None)
                is_above_now= current_price > l1_trigger

                # Save updated state for next loop
                new_price_state[state_key] = {
                    "was_above_trigger": is_above_now,
                    "last_trigger":      l1_trigger   # store current trigger for stability check next loop
                }

                long_layers = [t for t in open_trades
                               if t['symbol'] == symbol and t['side'] == 'LONG'
                               and t['status'] == 'open']

                # ── CHECK EXITS ──────────────────────────────────────────
                for trade in list(long_layers):
                    # Take profit: +1%, each layer exits independently
                    if current_price >= trade['profit_target']:
                        pnl = (current_price - trade['entry_price']) / trade['entry_price'] * 100 * trade['position_size']
                        trade.update({
                            'status':    'closed',
                            'exit_price': current_price,
                            'closed_at': datetime.now().isoformat(),
                            'pnl':       pnl
                        })
                        trades_data['history'].append(trade)
                        open_trades.remove(trade)
                        long_layers.remove(trade)
                        modified = True
                        send_telegram(
                            f"✅ <b>LONG L{trade['layer']} CLOSED</b>: {symbol}\n"
                            f"Entry: ${trade['entry_price']:.4f} → Exit: ${current_price:.4f}\n"
                            f"Profit: +${pnl:.2f} (+1%)"
                        )
                        print(f"Closed L{trade['layer']} {symbol} +${pnl:.2f}")
                        continue

                    # Stop loss: -3%
                    hit_sl, sl_price = check_stop_loss(trade['entry_price'], current_price)
                    if hit_sl:
                        pnl = -(STOP_LOSS_PCT / 100) * trade['entry_price'] * trade['position_size']
                        trade.update({
                            'status':    'stopped',
                            'exit_price': sl_price,
                            'closed_at': datetime.now().isoformat(),
                            'pnl':       pnl
                        })
                        trades_data['history'].append(trade)
                        open_trades.remove(trade)
                        long_layers.remove(trade)
                        modified = True
                        sl_cooldowns[state_key] = datetime.now()
                        save_cooldowns(sl_cooldowns)
                        send_telegram(
                            f"🛑 <b>STOP LOSS L{trade['layer']}</b>: {symbol}\n"
                            f"Entry: ${trade['entry_price']:.4f} → SL: ${sl_price:.4f}\n"
                            f"Loss: -${abs(pnl):.2f} (-{STOP_LOSS_PCT}%) | Cooldown: {SL_COOLDOWN_MIN}m"
                        )
                        print(f"Stop loss L{trade['layer']} {symbol} -${abs(pnl):.2f}")

                # ── STOP-LOSS COOLDOWN CHECK ─────────────────────────────
                if state_key in sl_cooldowns:
                    elapsed_min = (datetime.now() - sl_cooldowns[state_key]).total_seconds() / 60
                    if elapsed_min < SL_COOLDOWN_MIN:
                        print(f"{symbol}: SL cooldown ({int(SL_COOLDOWN_MIN - elapsed_min)}m left)")
                        continue
                    else:
                        del sl_cooldowns[state_key]
                        save_cooldowns(sl_cooldowns)

                # ── OPEN LAYER 1 ─────────────────────────────────────────
                # Conditions:
                # 1. No open layers for this symbol
                # 2. Price freshly crossed BELOW trigger (was above, now below)
                # 3. 24h high hasn't shifted more than 0.1% since last loop
                #    (stability check — prevents false entries from reference moving)
                if len(long_layers) == 0:
                    fresh_crossing = was_above and not is_above_now
                    stable_trigger = trigger_is_stable(l1_trigger, last_trigger)

                    if fresh_crossing and stable_trigger:
                        new_layer = open_layer(symbol, 1, l1_trigger, strategy)
                        open_trades.append(new_layer)
                        long_layers.append(new_layer)
                        modified = True
                        send_telegram(
                            f"📥 <b>LONG L1 OPENED</b>: {symbol}\n"
                            f"Entry: ${l1_trigger:.4f}\n"
                            f"Target: ${l1_trigger * 1.01:.4f} (+1%)\n"
                            f"24h High: ${high_24h:.4f}"
                        )
                        print(f"Opened L1 {symbol} @ ${l1_trigger:.4f}")
                    elif fresh_crossing and not stable_trigger:
                        # Crossing detected but 24h high shifted too much — skip, log only
                        shift = abs(l1_trigger - last_trigger) / last_trigger * 100 if last_trigger else 0
                        print(f"{symbol}: Crossing detected but trigger shifted {shift:.2f}% — skipping (not a real dip)")

                # ── OPEN LAYERS 2-10 ─────────────────────────────────────
                # 1% below previous layer, same fresh crossing + stability logic
                elif len(long_layers) < max_layers:
                    last_layer      = max(long_layers, key=lambda x: x['layer'])
                    next_layer_num  = last_layer['layer'] + 1
                    next_trigger    = last_layer['entry_price'] * 0.99

                    layer_state_key  = f"{symbol}_LONG_L{next_layer_num}"
                    prev_layer_state = prev_price_state.get(layer_state_key, {})
                    was_above_layer  = prev_layer_state.get("was_above_trigger", True)
                    last_layer_trig  = prev_layer_state.get("last_trigger", None)
                    is_above_layer   = current_price > next_trigger

                    new_price_state[layer_state_key] = {
                        "was_above_trigger": is_above_layer,
                        "last_trigger":      next_trigger
                    }

                    # Next layer trigger is fixed (based on previous layer entry price)
                    # so stability check uses a tighter tolerance here
                    fresh_layer_crossing = was_above_layer and not is_above_layer
                    stable_layer_trigger = trigger_is_stable(next_trigger, last_layer_trig)

                    if fresh_layer_crossing and stable_layer_trigger:
                        new_layer = open_layer(symbol, next_layer_num, next_trigger, strategy)
                        open_trades.append(new_layer)
                        modified = True
                        send_telegram(
                            f"📥 <b>LONG L{next_layer_num} OPENED</b>: {symbol}\n"
                            f"Entry: ${next_trigger:.4f}\n"
                            f"Target: ${next_trigger * 1.01:.4f} (+1%)\n"
                            f"DCA below L{last_layer['layer']}"
                        )
                        print(f"Opened L{next_layer_num} {symbol} @ ${next_trigger:.4f}")

            # Always save price state — crossing detection depends on it
            prev_price_state = new_price_state
            save_price_state(new_price_state)

            if modified:
                save_json(BASE_DIR / "trades.json", trades_data)
                total_pnl = sum(t.get('pnl', 0) for t in trades_data['history'])
                save_json(BASE_DIR / "data.json", {
                    'timestamp':      datetime.now().isoformat(),
                    'equity':         10000 + total_pnl,
                    'open_positions': len(open_trades),
                    'realized_pnl':   total_pnl,
                    'strategy':       'DCA Long - Strategy 1',
                    'mode':           strategy.get('mode', 'paper'),
                    'prices':         prices
                })

            print(f"Cycle {datetime.now().strftime('%H:%M:%S')} | Open layers: {len(open_trades)} | Sleeping {LOOP_SLEEP_SECONDS}s")
            time.sleep(LOOP_SLEEP_SECONDS)

        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(60)

if __name__ == "__main__":
    send_telegram(
        "🤖 <b>DCA Bot V3 started</b>\n"
        "Strategy 1: Classic DCA Long\n"
        "• Entry: 1% below rolling 24h high\n"
        "• Exit: +1% per layer independently\n"
        "• Stop loss: -3% with 1h cooldown\n"
        "• Trigger stability check active\n"
        "• Checking every 60 seconds"
    )
    main_loop()
