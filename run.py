#!/usr/bin/env python3
"""Main Entry Point: Delta Quant Core Trading Bot & Web Terminal.

Usage:
    python run.py             # Start the live trading server & dashboard on port 8000
    python run.py --port 8080 # Custom port
    python run.py --reload    # Auto-reload during development
"""
import sys
import argparse
import uvicorn

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Delta Quant Core Bot Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host binding (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code change")
    args = parser.parse_args()

    print("=" * 70)
    print("  [*] Starting Delta Quant Core Algorithmic Trading Server")
    print(f"  [+] Web Terminal Dashboard: http://localhost:{args.port}/dashboard")
    print(f"  [+] Interactive API Docs:   http://localhost:{args.port}/docs")
    print(f"  [+] Live WebSocket Feed:     ws://localhost:{args.port}/ws")
    print("=" * 70)

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)
