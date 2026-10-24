from typing import Optional
from pydantic import field_validator
try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError:
    from pydantic import BaseModel as BaseSettings  # type: ignore
    SettingsConfigDict = dict  # type: ignore

class Settings(BaseSettings):
    # Authenticated account/order requests stay on demo testnet by default.
    DELTA_BASE_URL: str = "https://cdn-ind.testnet.deltaex.org"
    # Paper trading reads live public prices/candles from production. This is
    # independent of DELTA_BASE_URL and does not enable real order execution.
    DELTA_MARKET_DATA_BASE_URL: str = "https://api.india.delta.exchange"
    # Public feed URL; DELTA_WS_URL remains for older configuration files.
    DELTA_PUBLIC_WS_URL: str = "wss://public-socket.india.delta.exchange"
    DELTA_WS_URL: str = "wss://socket-ind-pub.testnet.deltaex.org"
    DELTA_API_KEY: str = ""
    DELTA_API_SECRET: str = ""
    SYMBOL: str = "BTCUSD"
    TIMEFRAME: str = "1m"
    HTF_TIMEFRAME: str = "1h"
    RISK_PER_TRADE_PCT: float = 1.0
    MAX_LEVERAGE: int = 5
    MAX_OPEN_POSITIONS: int = 1
    DAILY_LOSS_LIMIT_PCT: float = 3.0
    MAX_DRAWDOWN_PCT: float = 10.0
    MAX_NOTIONAL_USD: float = 50000.0
    STALE_DATA_SECONDS: int = 120
    DEMO_MODE: bool = False
    PAPER_START_EQUITY: float = 10000.0
    PAPER_FEE_RATE: float = 0.0003
    PAPER_SLIPPAGE_BPS: float = 2.0
    PAPER_TEST_MAX_NOTIONAL_USD: float = 100.0
    DB_PATH: str = "trader.db"
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_NAME: str = "algo_bot2"
    DB_USER: str = "postgres"
    DB_PASSWORD: str = "2106"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @field_validator("DELTA_BASE_URL")
    @classmethod
    def validate_testnet_url(cls, v: str) -> str:
        if "testnet" not in v.lower():
            raise ValueError(f"DELTA_BASE_URL must be a testnet URL! Given: {v}")
        return v

    @field_validator("DELTA_MARKET_DATA_BASE_URL")
    @classmethod
    def validate_market_data_url(cls, v: str) -> str:
        allowed = ("https://api.india.delta.exchange", "https://cdn-ind.testnet.deltaex.org")
        if v.rstrip("/").lower() not in allowed:
            raise ValueError("DELTA_MARKET_DATA_BASE_URL must be a supported Delta India production or testnet URL")
        return v.rstrip("/")

settings = Settings()
