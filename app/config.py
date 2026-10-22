from typing import Optional
from pydantic import field_validator
try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError:
    from pydantic import BaseModel as BaseSettings  # type: ignore
    SettingsConfigDict = dict  # type: ignore

class Settings(BaseSettings):
    DELTA_BASE_URL: str = "https://cdn-ind.testnet.deltaex.org"
    # Public market data socket. DELTA_WS_URL is retained for compatibility with
    # older configuration files; the unauthenticated public feed has its own host.
    DELTA_PUBLIC_WS_URL: str = "wss://socket-ind-pub.testnet.deltaex.org"
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

settings = Settings()
