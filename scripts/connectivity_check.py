#!/usr/bin/env python3
"""Delta Exchange Connectivity Check (Role A).

Validates:
1. HMAC signature generation and REST API authentication.
2. Product resolution for target symbol (e.g. BTCUSD).
3. Historical candle fetching.
4. Account wallet balance and position retrieval.
5. Far-from-market limit order placement and immediate cancellation on testnet.
"""
import os
import sys
import time

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.exchange.client import DeltaClient

def run_connectivity_check():
    print("=" * 70)
    print("     DELTA EXCHANGE TESTNET CONNECTIVITY CHECK (ROLE A)")
    print("=" * 70)

    client = DeltaClient()
    print(f"\n[1/5] Target Endpoint: {client.base_url}")
    print(f"      API Key configured: {bool(client.api_key)}")

    # Step 1: Signature test
    ts, sig = client.generate_signature("GET", "/v2/wallet/balances")
    print(f"  [OK] HMAC-SHA256 signature generated: {sig[:16]}... (timestamp={ts})")

    # Step 2: Fetch Products
    print("\n[2/5] Fetching products from Delta Exchange...")
    try:
        products = client.get_products()
        print(f"  [OK] Successfully retrieved {len(products)} active products.")
        btc_product = client.get_product_by_symbol(settings.SYMBOL)
        if btc_product:
            prod_id = btc_product.get("id")
            print(f"  [OK] Resolved {settings.SYMBOL} -> Product ID {prod_id}")
        else:
            print(f"  [WARN] Symbol {settings.SYMBOL} not found in product catalogue.")
    except Exception as e:
        print(f"  [WARN] Failed to fetch products (Network/Auth): {e}")

    # Step 3: Fetch Historical Candles
    print(f"\n[3/5] Fetching 15m historical candles for {settings.SYMBOL}...")
    try:
        candles = client.get_candles(settings.SYMBOL, resolution="15m", limit=5)
        if candles:
            latest = candles[-1]
            print(f"  [OK] Retrieved {len(candles)} candles. Latest Close: ${latest.close:,.2f} at {latest.ts}")
        else:
            print("  [WARN] No candles returned from API.")
    except Exception as e:
        print(f"  [WARN] Candle fetch failed: {e}")

    # Step 4: Check Wallet Balance & Positions
    print("\n[4/5] Checking Wallet Balance & Positions...")
    if client.api_key and client.api_secret and "your_" not in client.api_key:
        try:
            balance = client.get_balance()
            print(f"  [OK] Wallet response: {balance}")
            positions = client.get_positions()
            print(f"  [OK] Active positions: {len(positions)}")
        except Exception as e:
            print(f"  [WARN] Balance/Position query failed: {e}")
    else:
        print("  [INFO] Skipping private balance query (Testnet API keys not configured).")

    # Step 5: Test Far-from-Market Order Placement & Cancellation
    print("\n[5/5] Testing Far-from-Market Limit Order Placement & Cancel...")
    if client.api_key and client.api_secret and "your_" not in client.api_key:
        try:
            prod_id = client.get_product_id(settings.SYMBOL)
            # Far below market: Buy 1 contract of BTC at $1,000
            order_res = client.place_order(
                product_id=prod_id,
                size=1.0,
                side="buy",
                order_type="limit_order",
                price=1000.0,
                client_order_id=f"conn-test-{int(time.time())}",
            )
            order_data = order_res.get("result", order_res)
            order_id = str(order_data.get("id", ""))
            print(f"  [OK] Placed test order ID: {order_id}")

            # Cancel immediately
            cancel_res = client.cancel_order(product_id=prod_id, order_id=order_id)
            print(f"  [OK] Cancelled test order ID: {order_id}")
        except Exception as e:
            print(f"  [WARN] Test order placement/cancellation failed: {e}")
    else:
        print("  [INFO] Skipping live order placement (Requires valid Testnet API keys in .env).")

    print("\n" + "=" * 70)
    print("     CONNECTIVITY CHECK COMPLETED")
    print("=" * 70)


if __name__ == "__main__":
    run_connectivity_check()
