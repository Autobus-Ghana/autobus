from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine.url import URL
import os
from typing import Optional


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"  # Ignore extra fields from .env that aren't defined in the model
    )
    
    SERVICE_NAME: str = "Autobus Backend"
    DEBUG: bool = os.environ.get("DEBUG", "false").lower() == "true"

    # Database Configuration - supports both traditional and Docker Postgres env vars
    DB_DRIVER: str = os.environ.get('DB_DRIVER', 'postgresql+asyncpg')
    DB_HOST: Optional[str] = os.environ.get('PGHOST') or os.environ.get('DB_HOST')
    DB_PORT: int = int(os.environ.get('PGPORT', os.environ.get('DB_PORT', 5432)))
    DB_USER: Optional[str] = os.environ.get('PGUSER') or os.environ.get('DB_USER')
    DB_PASSWORD: Optional[str] = os.environ.get('PGPASSWORD') or os.environ.get('DB_PASSWORD')
    DB_DATABASE: Optional[str] = os.environ.get('PGDATABASE') or os.environ.get('DB_DATABASE')
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 0
    DB_ECHO: bool = os.environ.get('DB_ECHO', 'false').lower() == 'true'

    # JWT Configuration — never ship with a known default secret in production
    SECRET_KEY: str = os.environ.get('SECRET_KEY', os.environ.get('JWT_SECRET_KEY', ''))
    ALGORITHM: str = os.environ.get('ALGORITHM', os.environ.get('JWT_ALGORITHM', 'HS256'))
    KID: str = os.environ.get('KID', 'autobus-kid')
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.environ.get('ACCESS_TOKEN_EXPIRE_MINUTES', 30))
    REFRESH_TOKEN_EXPIRE_MINUTES: int = 360  # legacy; AuthJWT config only
    # 7 days default (was 3650 ≈ 10 years)
    REFRESH_TOKEN_EXPIRE_DAYS: int = int(os.environ.get('REFRESH_TOKEN_EXPIRE_DAYS', 7))

    # Comma-separated browser origins for CORS (never use * with credentials)
    CORS_ORIGINS: str = os.environ.get(
        "CORS_ORIGINS",
        "https://useautobus.com,https://www.useautobus.com,https://admin.useautobus.com,http://localhost:8080,http://localhost:3000,http://localhost:4200",
    )

    # Platform admins (user ids and/or emails). Falls back to ADMIN_NOTIFICATION_USER_IDS.
    ADMIN_USER_IDS: str = os.environ.get("ADMIN_USER_IDS", "")
    ADMIN_EMAILS: str = os.environ.get("ADMIN_EMAILS", "")

    # Shared secret for public marketing/webhook helpers (optional but recommended)
    PUBLIC_WEBHOOK_API_KEY: str = os.environ.get("PUBLIC_WEBHOOK_API_KEY", "").strip()
    META_APP_SECRET: str = os.environ.get("META_APP_SECRET", "").strip()
    REQUIRE_TOKEN_ENCRYPTION: bool = os.environ.get(
        "REQUIRE_TOKEN_ENCRYPTION", "true"
    ).lower() == "true"
    
    # Redis Configuration
    REDIS_HOST: str = os.environ.get('REDIS_HOST', 'localhost')
    REDIS_PORT: str = os.environ.get('REDIS_PORT', '6379')
    REDIS_PASSWORD: str = os.environ.get('REDIS_PASSWORD', '')
    
    # Message Queue Configuration
    RABBIT_MQ_URL: str = os.environ.get('RABBIT_MQ_URL', '')
    RABBIT_MQ_ROUTING_KEY: str = os.environ.get('RABBIT_MQ_ROUTING_KEY', '')
    RABBIT_MQ_AUDIT_QUEUE: str = os.environ.get('RABBIT_MQ_AUDIT_QUEUE', '')
    SMS_MQ_QUEUE: str = os.environ.get('SMS_MQ_QUEUE', '')
    EMAIL_MQ_QUEUE: str = os.environ.get('EMAIL_MQ_QUEUE', '')
    BASE_FRONTEND_URL: str = os.environ.get('BASE_FRONTEND_URL', 'http://localhost:3000')
    BATCH_CUSTOMER_UPLOAD_QUEUE: str = os.environ.get('BATCH_CUSTOMER_UPLOAD_QUEUE', '')
    COMPANY_QUEUE: str = os.environ.get('COMPANY_QUEUE', '')

    # Wirepick SMS Configuration
    # Note: some env files include accidental surrounding quotes; we normalize via .strip().
    WIREPICK_API_URL: str = os.environ.get("WIREPICK_API_URL", "https://api.wirepick.com/httpsms").strip().strip('"').strip("'")
    WIREPICK_CLIENT_ID: str = os.environ.get("WIREPICK_CLIENT_ID", "").strip()
    WIREPICK_PASSWORD: str = os.environ.get("WIREPICK_PASSWORD", "").strip()
    WIREPICK_PUBLIC_KEY: str = os.environ.get("WIREPICK_PUBLIC_KEY", "").strip()
    WIREPICK_SENDER_ID: str = os.environ.get("WIREPICK_SENDER_ID", "AutoBus").strip()
    USE_WIREPICK_API_KEY: bool = os.environ.get("USE_WIREPICK_API_KEY", "false").lower() == "true"

    # Email (SMTP) configuration (used for OTP email + agent email tool)
    ZEPTOMAIL_SMTP_HOST: str = os.environ.get("ZEPTOMAIL_SMTP_HOST", "smtp.zeptomail.com").strip()
    ZEPTOMAIL_SMTP_PORT: int = int(os.environ.get("ZEPTOMAIL_SMTP_PORT", 587))
    ZEPTOMAIL_SMTP_USERNAME: str = os.environ.get("ZEPTOMAIL_SMTP_USERNAME", "emailapikey").strip()
    # Some envs store Zeptomail SMTP password as API token.
    # Note: pydantic-settings will bind empty ZEPTOMAIL_SMTP_PASSWORD="" from Docker and
    # override class defaults, so we also fall back in a model_validator below.
    ZEPTOMAIL_SMTP_PASSWORD: str = (
        os.environ.get("ZEPTOMAIL_SMTP_PASSWORD")
        or os.environ.get("ZEPTOMAIL_API_TOKEN")
        or ""
    ).strip()
    ZEPTOMAIL_FROM_EMAIL: str = os.environ.get("ZEPTOMAIL_FROM_EMAIL", "").strip()
    ZEPTOMAIL_SENDER_DOMAIN: str = os.environ.get("ZEPTOMAIL_SENDER_DOMAIN", "useautobus.com").strip()

    @model_validator(mode="after")
    def _zeptomail_password_fallback(self):
        if not (self.ZEPTOMAIL_SMTP_PASSWORD or "").strip():
            token = (os.environ.get("ZEPTOMAIL_API_TOKEN") or "").strip()
            if token:
                object.__setattr__(self, "ZEPTOMAIL_SMTP_PASSWORD", token)
        return self
    
    # Blotato Social Media Integration Configuration
    BLOTATO_API_KEY: str = os.environ.get('BLOTATO_API_KEY', '')
    BLOTATO_CLIENT_ID: str = os.environ.get('BLOTATO_CLIENT_ID', '')
    BLOTATO_CLIENT_SECRET: str = os.environ.get('BLOTATO_CLIENT_SECRET', '')
    BLOTATO_API_BASE: str = os.environ.get('BLOTATO_API_BASE', 'https://api.blotato.com')
    BLOTATO_OAUTH_BASE: str = os.environ.get('BLOTATO_OAUTH_BASE', 'https://app.blotato.com')
    
    # Self-hosted n8n workflow engine (admin portal builder). The API key never leaves the backend.
    N8N_BASE_URL: str = os.environ.get("N8N_BASE_URL", "http://n8n:5678").strip().rstrip("/")
    N8N_API_KEY: str = os.environ.get("N8N_API_KEY", "").strip()
    N8N_PUBLIC_URL: str = os.environ.get("N8N_PUBLIC_URL", "https://n8n.useautobus.com").strip().rstrip("/")

    # OTP Configuration
    # Prefer OTP_EXPIRE_SECONDS; if unset, honor OTP_EXPIRE_MINUTES (compose default 5).
    # Fallback is 5 minutes — 30s was too short for SMS delivery + user entry.
    OTP_EXPIRE_SECONDS: int = (
        int(os.environ["OTP_EXPIRE_SECONDS"])
        if os.environ.get("OTP_EXPIRE_SECONDS")
        else int(float(os.environ.get("OTP_EXPIRE_MINUTES", "5")) * 60)
    )
    # Backward-compatible minutes value for any legacy call sites.
    OTP_EXPIRE_MINUTES: float = OTP_EXPIRE_SECONDS / 60

    # MongoDB Logging
    MONGO_URI: str = os.environ.get('MONGO_URI', 'mongodb://localhost:27017/')
    MONGO_DB_NAME: str = os.environ.get('MONGO_DB_NAME', 'api_logs_db')
    
    # Logging levels
    LOG_LEVEL: str = os.environ.get('LOG_LEVEL', 'INFO')

    # Rate limiting (Redis fixed-window; see utilities/rate_limit.py)
    RATE_LIMIT_ENABLED: bool = os.environ.get("RATE_LIMIT_ENABLED", "true").lower() == "true"
    RATE_LIMIT_WINDOW_SECONDS: int = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", 60))

    # Comma-separated users.id values that receive admin inbox notifications
    ADMIN_NOTIFICATION_USER_IDS: str = os.environ.get("ADMIN_NOTIFICATION_USER_IDS", "")
    SMS_NOTIFICATION_ENABLED: bool = os.environ.get("SMS_NOTIFICATION_ENABLED", "true").lower() == "true"

    @model_validator(mode="after")
    def _require_jwt_secret(self):
        key = (self.SECRET_KEY or "").strip()
        if len(key) < 32:
            if self.DEBUG:
                # Local-only fallback; never acceptable in production
                object.__setattr__(
                    self,
                    "SECRET_KEY",
                    key or "dev-only-insecure-secret-change-me!!",
                )
            else:
                raise ValueError(
                    "SECRET_KEY / JWT_SECRET_KEY must be set to a strong value "
                    "(>= 32 characters) when DEBUG is false"
                )
        return self

    # Paystack (standalone billing checkout)
    PAYSTACK_SECRET_KEY: str = os.environ.get("PAYSTACK_SECRET_KEY", "").strip()
    PAYSTACK_BILLING_CALLBACK_URL: str = os.environ.get("PAYSTACK_BILLING_CALLBACK_URL", "").strip()
    # Optional lock for USD→GHS. Leave 0 to use a live FX rate at credit checkout.
    USD_GHS_RATE: float = float(os.environ.get("USD_GHS_RATE", "0") or 0)

    # Apple In-App Purchase (StoreKit 2)
    APPLE_BUNDLE_ID: str = os.environ.get("APPLE_BUNDLE_ID", "com.autobus.app").strip()
    APPLE_IAP_PRODUCT_PREFIX: str = os.environ.get("APPLE_IAP_PRODUCT_PREFIX", "autobus").strip()
    APPLE_IAP_PRODUCT_MAP: str = os.environ.get("APPLE_IAP_PRODUCT_MAP", "").strip()
    APPLE_IAP_ALLOW_SANDBOX: str = os.environ.get("APPLE_IAP_ALLOW_SANDBOX", "").strip()

    # Google Play Billing (Android consumable credits)
    GOOGLE_PLAY_PACKAGE_NAME: str = os.environ.get(
        "GOOGLE_PLAY_PACKAGE_NAME", "com.autobus.app"
    ).strip()
    GOOGLE_PLAY_SERVICE_ACCOUNT_JSON: str = os.environ.get(
        "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", ""
    ).strip()
    GOOGLE_PLAY_SERVICE_ACCOUNT_FILE: str = os.environ.get(
        "GOOGLE_PLAY_SERVICE_ACCOUNT_FILE", ""
    ).strip()

    @property
    def DB_DSN(self) -> URL:
        return URL.create(
            self.DB_DRIVER,
            self.DB_USER,
            self.DB_PASSWORD,
            self.DB_HOST,
            self.DB_PORT,
            self.DB_DATABASE,
        )

    @property
    def DB_URL_STRING(self) -> str:
        return f'{self.DB_DRIVER}://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_DATABASE}?async_fallback=true'

    def MULTI_TENANT_DB_STRING(self, migration_id: str) -> str:
        return (f'jdbc:postgresql://{self.DB_HOST}:'
                f'{self.DB_PORT}/{migration_id}?ApplicationName=MultiTenant')


settings = Settings()