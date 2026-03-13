#!/usr/bin/env python3
"""
Aster Pilot Fund - DCA Trading Bot V3
Clean implementation with:
- Fixed thresholds (no recalculation spam)
- Stop-loss cooldown (no immediate re-entry)
- Duplicate layer protection
"""

import json
import requests
import time
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent

# Telegram Config
TELEGRAM_BOT_TOKEN = "8777890597:AAEeWR6AnVeO6rO4WBOWAVtbIvVu9jFhbtw"
TELEGRAM_CHAT_ID = "1058007741"

def send_telegram(message):
    """Send notification via Telegram"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        requests.post(url, json=data, timeout=5)
    except Exception as e:
        print(f"Telegram error: {e}")

def load_json(filepath, default=None):
    """Load JSON file"""
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except:
        return default if default is not None else {}

def save_json(filepath, data):
    """Save JSON file"""
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2, default=str)

def get_prices():
    """Fetch current prices from Aster DEX"""
    try:
        resp = requests.get("https://fapi.asterdex.com/fapi/v1/ticker/24hr", timeout=10)
        tickers = resp.json()
        
        prices = {}
        for t in tickers:
            symbol = t['symbol']
            prices[symbol] = {
                'price': float(t['lastPrice']),
                'high_24h': float(t['highPrice']),
                'low_24h': float(t['lowPrice']),
                'change_pct': float(t['priceChangePercent']),
                'volume': float(t['volume'])
            }
        return prices
    except Exception as e:
        print(f"Price fetch error: {e}")
        return {}

def check_stop_loss(entry, current_price, side):
    """Check if stop-loss hit (3% loss per layer)"""
    stop_loss_pct = 3
    
    if side == 'LONG':
        loss_pct = (entry - current_price) / entry * 100
        if loss_pct >= stop_loss_pct:
            return True, entry * 0.97
    else:  # SHORT
        loss_pct = (current_price - entry) / entry * 100
        if loss_pct >= stop_loss_pct:
            return True, entry * 1.03
    
    return False, None

def open_layer(symbol, layer_num, entry_price, side, strategy):
    """
    Open a new DCA layer.
    Entry price is PASSED IN (fixed at creation time, never recalculated).
    """
    rules = strategy['dca']
    position_size = rules.get('layer_size', 50)
    leverage = rules.get('leverage', 50)
    
    # Profit target: +1% for LONG, -1% for SHORT
    if side == 'LONG':
        profit_target = entry_price * 1.01
    else:
        profit_target = entry_price * 0.99
    
    return {
        'symbol': symbol,
        'side': side,
        'layer': layer_num,
        'entry_price': entry_price,
        'profit_target': profit_target,
        'position_size': position_size,
        'leverage': leverage,
        'status': 'open',
        'opened_at': datetime.now().isoformat(),
        'unrealized_pnl': 0
    }

def main_loop():
    """Main trading loop"""
    print("🤖 DCA Bot V3 started (no spam, no cooldown issues)")
    
    # Track stop-loss cooldowns to prevent immediate re-entry
    sl_cooldowns = {}  # {symbol_side: datetime}
    sl_cooldown_minutes = 60  # 1 hour
    
    while True:
        try:
            # Load config
            strategy = load_json(BASE_DIR / "strategy.json")
            trades_data = load_json(BASE_DIR / "trades.json", {"open": [], "history": []})
            
            # Kill switch
            if strategy.get('kill_switch'):
                print("⛔ Kill switch ON")
                time.sleep(60)
                continue
            
            # Get prices
            prices = get_prices()
            if not prices:
                print("⚠️ No price data")
                time.sleep(60)
                continue
            
            symbols = strategy['dca']['symbols']
            max_layers = strategy['dca']['max_layers']
            open_trades = trades_data['open']
            
            # Global safety: max 50 total layers
            if len(open_trades) >= 50:
                print("⚠️ Max 50 layers reached")
                time.sleep(60)
                continue
            
            modified = False
            
            for symbol in symbols:
                if symbol not in prices:
                    continue
                
                current_price = prices[symbol]['price']
                high_24h = prices[symbol]['high_24h']
                low_24h = prices[symbol]['low_24h']
                
                # Get layers for this symbol
                long_layers = [t for t in open_trades if t['symbol'] == symbol and t['side'] == 'LONG' and t['status'] == 'open']
                short_layers = [t for t in open_trades if t['symbol'] == symbol and t['side'] == 'SHORT' and t['status'] == 'open']
                
                # ===== CHECK EXITS =====
                for trade in long_layers + short_layers:
                    # Take profit
                    if trade['side'] == 'LONG' and current_price >= trade['profit_target']:
                        pnl = (current_price - trade['entry_price']) / trade['entry_price'] * 100 * trade['position_size']
                        trade['status'] = 'closed'
                        trade['exit_price'] = current_price
                        trade['closed_at'] = datetime.now().isoformat()
                        trade['pnl'] = pnl
                        trades_data['history'].append(trade)
                        open_trades.remove(trade)
                        modified = True
                        
                        msg = f"✅ {trade['side']} L{trade['layer']}: {symbol} +${pnl:.2f}"
                        send_telegram(msg)
                        
                    elif trade['side'] == 'SHORT' and current_price <= trade['profit_target']:
                        pnl = (trade['entry_price'] - current_price) / trade['entry_price'] * 100 * trade['position_size']
                        trade['status'] = 'closed'
                        trade['exit_price'] = current_price
                        trade['closed_at'] = datetime.now().isoformat()
                        trade['pnl'] = pnl
                        trades_data['history'].append(trade)
                        open_trades.remove(trade)
                        modified = True
                        
                        msg = f"✅ {trade['side']} L{trade['layer']}: {symbol} +${pnl:.2f}"
                        send_telegram(msg)
                    
                    # Stop loss
                    hit_sl, sl_price = check_stop_loss(trade['entry_price'], current_price, trade['side'])
                    if hit_sl:
                        pnl = -3 * trade['position_size']
                        trade['status'] = 'stopped'
                        trade['exit_price'] = sl_price
                        trade['closed_at'] = datetime.now().isoformat()
                        trade['pnl'] = pnl
                        trades_data['history'].append(trade)
                        open_trades.remove(trade)
                        modified = True
                        
                        # Add cooldown
                        cooldown_key = f"{symbol}_{trade['side']}"
                        sl_cooldowns[cooldown_key] = datetime.now()
                        
                        msg = f"🛑 {trade['side']} L{trade['layer']}: {symbol} -${abs(pnl):.2f} (cooldown: 1h)"
                        send_telegram(msg)
                
                # ===== CHECK ENTRIES (LONG) =====
                if len(long_layers) < max_layers:
                    # Check cooldown
                    cooldown_key = f"{symbol}_LONG"
                    if cooldown_key in sl_cooldowns:
                        elapsed_min = (datetime.now() - sl_cooldowns[cooldown_key]).total_seconds() / 60
                        if elapsed_min < sl_cooldown_minutes:
                            continue  # Skip this symbol - in cooldown
                        else:
                            del sl_cooldowns[cooldown_key]  # Expired
                    
                    # Check if next layer number already exists
                    existing_layers = [t['layer'] for t in long_layers]
                    next_layer_num = len(long_layers) + 1
                    
                    if next_layer_num not in existing_layers:
                        if len(long_layers) == 0:
                            # Layer 1: 1% below 24h high
                            entry_price = high_24h * 0.99
                            if current_price <= entry_price:
                                new_layer = open_layer(symbol, 1, entry_price, 'LONG', strategy)
                                open_trades.append(new_layer)
                                modified = True
                                send_telegram(f"📥 LONG L1: {symbol} @ ${entry_price:.2f}")
                        else:
                            # Layer 2+: 1% below previous layer (FIXED)
                            last_layer = max(long_layers, key=lambda x: x['layer'])
                            entry_price = last_layer['entry_price'] * 0.99
                            if current_price <= entry_price:
                                new_layer = open_layer(symbol, next_layer_num, entry_price, 'LONG', strategy)
                                open_trades.append(new_layer)
                                modified = True
                                send_telegram(f"📥 LONG L{next_layer_num}: {symbol} @ ${entry_price:.2f}")
                
                # ===== CHECK ENTRIES (SHORT) =====
                if len(short_layers) < max_layers:
                    # Check cooldown
                    cooldown_key = f"{symbol}_SHORT"
                    if cooldown_key in sl_cooldowns:
                        elapsed_min = (datetime.now() - sl_cooldowns[cooldown_key]).total_seconds() / 60
                        if elapsed_min < sl_cooldown_minutes:
                            continue
                        else:
                            del sl_cooldowns[cooldown_key]
                    
                    # Check if next layer number already exists
                    existing_layers = [t['layer'] for t in short_layers]
                    next_layer_num = len(short_layers) + 1
                    
                    if next_layer_num not in existing_layers:
                        if len(short_layers) == 0:
                            # Layer 1: 1% above 24h low
                            entry_price = low_24h * 1.01
                            if current_price >= entry_price:
                                new_layer = open_layer(symbol, 1, entry_price, 'SHORT', strategy)
                                open_trades.append(new_layer)
                                modified = True
                                send_telegram(f"📥 SHORT L1: {symbol} @ ${entry_price:.2f}")
                        else:
                            # Layer 2+: 1% above previous layer (FIXED)
                            last_layer = max(short_layers, key=lambda x: x['layer'])
                            entry_price = last_layer['entry_price'] * 1.01
                            if current_price >= entry_price:
                                new_layer = open_layer(symbol, next_layer_num, entry_price, 'SHORT', strategy)
                                open_trades.append(new_layer)
                                modified = True
                                send_telegram(f"📥 SHORT L{next_layer_num}: {symbol} @ ${entry_price:.2f}")
            
            # Save if modified
            if modified:
                save_json(BASE_DIR / "trades.json", trades_data)
                
                # Update dashboard
                total_pnl = sum(t.get('pnl', 0) for t in trades_data['history'])
                dashboard = {
                    'timestamp': datetime.now().isoformat(),
                    'equity': 10000 + total_pnl,
                    'open_positions': len(open_trades),
                    'realized_pnl': total_pnl,
                    'strategy': 'DCA Long + Short',
                    'mode': strategy.get('mode', 'paper'),
                    'prices': prices
                }
                save_json(BASE_DIR / "data.json", dashboard)
            
            # Sleep 3 seconds
            time.sleep(3)
            
        except Exception as e:
            print(f"❌ Error: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(60)

if __name__ == "__main__":
    send_telegram("🤖 DCA Bot V3 started")
    main_loop()
