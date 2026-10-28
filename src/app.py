from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from another_fastapi_jwt_auth import AuthJWT
from fastapi.middleware.cors import CORSMiddleware
from pydantic_settings import BaseSettings
import sys
import os

# Add the current directory to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import exceptions
from routes import base_routes
from core.auth.controller.authcontroller import auth_routes
from core.user.controller.usercontroller import user_routes
from core.cloudstorage.controller.storagecontoller import storage_routes
from core.notification.controller.notificationcontroller import notification_routes
from core.payments.controller.billcontroller import bill_routes
from core.billing.controller.billing_controller import billing_routes
from core.payments.controller.invoicecontroller import invoice_routes
from core.payments.controller.paymentcontroller import payment_routes
from core.otp.controller.otpcontroller import otp_routes
from core.subscription.controller.subscription_controller import subscription_routes
from core.iap.controller.apple_iap_controller import apple_iap_routes
from core.credits.controller.credit_controller import credit_routes
from core.webhooks.controller.webhookscontroller import webhooks_routes
from core.customers.controller.customer_controller import customer_routes
from core.nlu.controller.nlucontroller import nlu_routes
from core.paystack.controller.paystack_controller import paystack_routes
from core.agent.controller.agentcontroller import agent_routes
from core.media.controller.media_controller import media_routes
from core.socialmedia.controller.socialmedia_controller import social_routes
from core.whatsapp.controller.whatsapp_controller import (
    meta_whatsapp_callback_routes,
    whatsapp_routes,
)
from core.instagram.controller.instagram_controller import instagram_routes
from core.sms_sender_id.controller.sms_sender_id_controller import sms_sender_id_routes
from core.chatwoot.controller.chatwoot_controller import chatwoot_routes
from core.integrations.controller.integration_test_controller import integration_routes
from core.product.controller.product_controller import product_routes
from core.orders.controller.order_controller import order_routes
from core.interventions.controller.intervention_controller import intervention_routes
from core.conversationmanager.controller.conversation_controller import conversation_routes
from core.intelligence.controller.intelligence_controller import intelligence_routes
from core.admin.controller.admin_controller import (
    admin_routes,
    owner_resource_routes,
    public_ads_routes,
    public_automation_routes,
)
from core.ai_workflow.controller.ai_workflow_controller import ai_workflow_routes
from core.embed.controller.embed_controller import embed_routes, portal_embed_routes
from core.n8n.controller.n8n_controller import n8n_routes
from core.workflow_actions.controller import workflow_action_routes

from utilities.dbconfig import Base, engine
from config import settings
from utilities.exceptions import DatabaseValidationError
from fastapi.exceptions import RequestValidationError
from sqlalchemy import inspect

from loguru import logger
from contextlib import asynccontextmanager


# Initialize FastAPI with lifespan event handler
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle application startup and shutdown"""
    # Startup
    logger.info("[APP_STARTUP] Application starting...")
    try:
        import utilities.dbmodels  # noqa: F401 — register all ORM models
        try:
            Base.metadata.create_all(bind=engine)
        except Exception as create_err:
            logger.warning("[APP_STARTUP] metadata.create_all skipped: %s", create_err)
        from sqlalchemy import inspect, text
        from core.credits.service.credit_service import CreditService
        from utilities.dbconfig import SessionLocal

        insp = inspect(engine)
        if "subscription_plans" in insp.get_table_names():
            cols = {c["name"] for c in insp.get_columns("subscription_plans")}
            if "credit_allocations" not in cols:
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            "ALTER TABLE subscription_plans "
                            "ADD COLUMN credit_allocations TEXT"
                        )
                    )
                logger.info("[APP_STARTUP] Added subscription_plans.credit_allocations column")
            for col_name, col_sql in (
                ("apple_product_id_monthly", "VARCHAR(255)"),
                ("apple_product_id_yearly", "VARCHAR(255)"),
            ):
                if col_name not in cols:
                    with engine.begin() as conn:
                        conn.execute(
                            text(
                                f"ALTER TABLE subscription_plans ADD COLUMN {col_name} {col_sql}"
                            )
                        )
                    logger.info(f"[APP_STARTUP] Added subscription_plans.{col_name} column")

        if "user_subscriptions" in insp.get_table_names():
            sub_cols = {c["name"] for c in insp.get_columns("user_subscriptions")}
            for col_name, col_sql in (
                ("payment_provider", "VARCHAR(32)"),
                ("apple_original_transaction_id", "VARCHAR(255)"),
                ("apple_product_id", "VARCHAR(255)"),
            ):
                if col_name not in sub_cols:
                    with engine.begin() as conn:
                        conn.execute(
                            text(
                                f"ALTER TABLE user_subscriptions ADD COLUMN {col_name} {col_sql}"
                            )
                        )
                    logger.info(f"[APP_STARTUP] Added user_subscriptions.{col_name} column")

        if "users" in insp.get_table_names():
            user_cols = {c["name"] for c in insp.get_columns("users")}
            if "onboarding_profile" not in user_cols:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE users ADD COLUMN onboarding_profile JSONB"))
                logger.info("[APP_STARTUP] Added users.onboarding_profile column")
            if "onboarding_completed" not in user_cols:
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            "ALTER TABLE users ADD COLUMN onboarding_completed "
                            "BOOLEAN NOT NULL DEFAULT TRUE"
                        )
                    )
                logger.info("[APP_STARTUP] Added users.onboarding_completed column")
            if "currency_code" not in user_cols:
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            "ALTER TABLE users ADD COLUMN currency_code "
                            "VARCHAR(3) NOT NULL DEFAULT 'GHS'"
                        )
                    )
                logger.info("[APP_STARTUP] Added users.currency_code column")
            if "platform_role" not in user_cols:
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            "ALTER TABLE users ADD COLUMN platform_role VARCHAR(32)"
                        )
                    )
                    conn.execute(
                        text(
                            "CREATE INDEX IF NOT EXISTS ix_users_platform_role "
                            "ON users (platform_role)"
                        )
                    )
                logger.info("[APP_STARTUP] Added users.platform_role column")
            if "referral_code" not in user_cols:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE users ADD COLUMN referral_code VARCHAR(16)"))
                    conn.execute(
                        text(
                            "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_referral_code "
                            "ON users (referral_code) WHERE referral_code IS NOT NULL"
                        )
                    )
                logger.info("[APP_STARTUP] Added users.referral_code column")
            if "referred_by_user_id" not in user_cols:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE users ADD COLUMN referred_by_user_id VARCHAR(20)"))
                    conn.execute(
                        text(
                            "CREATE INDEX IF NOT EXISTS ix_users_referred_by_user_id "
                            "ON users (referred_by_user_id)"
                        )
                    )
                logger.info("[APP_STARTUP] Added users.referred_by_user_id column")
            if "managed_by_user_id" not in user_cols:
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            "ALTER TABLE users ADD COLUMN managed_by_user_id VARCHAR(20)"
                        )
                    )
                    conn.execute(
                        text(
                            "CREATE INDEX IF NOT EXISTS ix_users_managed_by_user_id "
                            "ON users (managed_by_user_id)"
                        )
                    )
                logger.info("[APP_STARTUP] Added users.managed_by_user_id column")

        if "products" in insp.get_table_names():
            product_cols = {c["name"] for c in insp.get_columns("products")}
            for col_name, col_sql in (
                ("external_id", "VARCHAR(128)"),
                ("kind", "VARCHAR(16) NOT NULL DEFAULT 'product'"),
                ("is_active", "BOOLEAN NOT NULL DEFAULT TRUE"),
                ("stock_tracked", "BOOLEAN NOT NULL DEFAULT TRUE"),
                ("sub_business_id", "VARCHAR(128)"),
            ):
                if col_name not in product_cols:
                    with engine.begin() as conn:
                        conn.execute(text(f"ALTER TABLE products ADD COLUMN {col_name} {col_sql}"))
                    logger.info("[APP_STARTUP] Added products.%s column", col_name)
            with engine.begin() as conn:
                conn.execute(text("DROP INDEX IF EXISTS uq_products_user_external_id"))
                conn.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_products_user_sub_external "
                        "ON products (user_id, COALESCE(sub_business_id, ''), external_id) "
                        "WHERE external_id IS NOT NULL"
                    )
                )

        if "product_images" in insp.get_table_names():
            image_cols = {c["name"] for c in insp.get_columns("product_images")}
            for col_name, col_sql in (
                ("match_code", "VARCHAR(16)"),
                ("phash", "VARCHAR(32)"),
                ("published_phash", "VARCHAR(32)"),
                ("visual_vector", "TEXT"),
            ):
                if col_name not in image_cols:
                    with engine.begin() as conn:
                        conn.execute(
                            text(f"ALTER TABLE product_images ADD COLUMN {col_name} {col_sql}")
                        )
                    logger.info("[APP_STARTUP] Added product_images.%s column", col_name)
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_product_images_match_code "
                        "ON product_images (match_code) WHERE match_code IS NOT NULL"
                    )
                )

        if "otps" in insp.get_table_names():
            try:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE otps ALTER COLUMN otp TYPE VARCHAR(128)"))
                logger.info("[APP_STARTUP] Ensured otps.otp column supports hashed OTPs")
            except Exception as alter_err:
                logger.debug("[APP_STARTUP] otps.otp alter skipped: %s", alter_err)

        db = SessionLocal()
        try:
            from core.credits.service.credit_pricing_service import CreditPricingService

            CreditPricingService(db).ensure_defaults()
            synced = CreditService(db).sync_plan_credit_allocations()
            if synced:
                logger.info(f"[APP_STARTUP] Synced credit allocations for {synced} plan(s)")
            from sqlalchemy import func as sa_func, or_ as sa_or
            from core.user.model.User import User as UserModel
            admin_emails = [
                e.strip().lower()
                for e in (settings.ADMIN_EMAILS or "").split(",")
                if e.strip()
            ]
            if admin_emails:
                seeded = (
                    db.query(UserModel)
                    .filter(sa_func.lower(UserModel.email).in_(admin_emails))
                    .filter(sa_or(UserModel.platform_role.is_(None), UserModel.platform_role == ""))
                    .update({UserModel.platform_role: "super_admin"}, synchronize_session=False)
                )
                if seeded:
                    db.commit()
                    logger.info("[APP_STARTUP] Seeded platform_role for %s env admin(s)", seeded)
            try:
                from core.admin.service.owner_resource_service import seed_owner_resources

                added = seed_owner_resources(db)
                if added:
                    logger.info("[APP_STARTUP] Seeded %s owner resources", added)
            except Exception as seed_err:
                logger.warning("[APP_STARTUP] Owner resource seed skipped: %s", seed_err)
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"[APP_STARTUP] Credit table init skipped: {e}")
    try:
        from core.socialmedia.service.zernio_service import ensure_zernio_webhook

        await ensure_zernio_webhook()
    except Exception as zernio_err:
        logger.warning("[APP_STARTUP] Zernio webhook setup skipped: %s", zernio_err)
    yield
    # Shutdown
    logger.info("[APP_SHUTDOWN] Application shutting down...")
    try:
        from core.payments.service.payment_check_service import PaymentCheckService
        PaymentCheckService.shutdown_scheduler()
    except Exception as e:
        logger.error(f"[APP_SHUTDOWN_ERROR] Error shutting down scheduler: {str(e)}")


app = FastAPI(
    title=settings.SERVICE_NAME,
    version="1.0",
    description="""**Autobus Core API** An AI focused app infrastructure deployed with python.

    Default Endpoints:
    - Authentication
    - File and Document Management
    - Message and Task Queuing
    - Notifications
    """,
    contact={
        "name": "API Support",
        "url": "http://support@autobus.com",
        "email": "mail@autobus.com",
    },
    license_info={
        "name": "MIT",
    },
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    openapi_url="/openapi.json" if settings.DEBUG else None,
)


@app.get("/health")
async def root_health():
    """Docker / load-balancer probe."""
    return {"status": "healthy", "service": settings.SERVICE_NAME}


@app.get("/", include_in_schema=False)
async def api_root():
    if settings.DEBUG:
        return RedirectResponse(url="/docs")
    return {"status": "ok", "service": settings.SERVICE_NAME}


@app.get("/swagger", include_in_schema=False)
async def swagger_redirect():
    if settings.DEBUG:
        return RedirectResponse(url="/docs")
    from fastapi import HTTPException
    raise HTTPException(status_code=404, detail="Not found")


# -----------------------------------------------------------
# Middleware (CORS) — explicit origins only (never * + credentials)
# Rate limiting is registered after CORS so it executes first on the request.
# -----------------------------------------------------------
_cors_origins = [
    o.strip() for o in (settings.CORS_ORIGINS or "").split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["https://useautobus.com"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-Requested-With", "X-Api-Key"],
)
try:
    from utilities.rate_limit import RateLimitMiddleware

    app.add_middleware(RateLimitMiddleware)
except ImportError:
    pass


# Exception Handlers

app.add_exception_handler(DatabaseValidationError, exceptions.database_validation_exception_handler)
app.add_exception_handler(RequestValidationError, exceptions.validation_exception_handler)

# Routes Registration

app.include_router(base_routes, prefix="/api/v1", tags=["Base Routes"])
app.include_router(storage_routes, prefix="/api/v1/storage", tags=["Storage Routes"])
app.include_router(auth_routes, prefix="/api/v1/auth", tags=["Auth Routes"])
app.include_router(user_routes, prefix="/api/v1/user", tags=["User Routes"])
app.include_router(notification_routes, prefix="/api/v1/notification", tags=["Notification Routes"])
app.include_router(payment_routes, prefix="/api/v1/payment", tags=["Payment Routes"])
app.include_router(bill_routes, prefix="/api/v1/bill", tags=["Billing Routes (legacy)"])
app.include_router(billing_routes, prefix="/api/v1/billing", tags=["Billing Service"])
app.include_router(invoice_routes, prefix="/api/v1/invoice", tags=["Invoice Routes"])
app.include_router(otp_routes, prefix="/api/v1/otp", tags=["OTP Routes"])
app.include_router(subscription_routes, prefix="/api/v1/subscription", tags=["Subscription Routes"])
app.include_router(apple_iap_routes, prefix="/api/v1/iap", tags=["Apple In-App Purchase"])
app.include_router(credit_routes, prefix="/api/v1/credits", tags=["Credit Routes"])
app.include_router(customer_routes, prefix="/api/v1/customers", tags=["Customer Routes"])
app.include_router(webhooks_routes, prefix="/api/v1/webhooks", tags=["Webhooks Routes"])
app.include_router(nlu_routes, prefix="/api/v1/nlu", tags=["NLU Routes"])
app.include_router(paystack_routes, prefix="/api/v1/paystack", tags=["Paystack Routes"])
app.include_router(agent_routes, prefix="/api/v1/agent", tags=["Agent Routes"])
app.include_router(agent_routes, prefix="/api/v1/agents", tags=["Agent Routes (portal)"])
app.include_router(media_routes, prefix="/api/v1/media", tags=["Media Generation"])
app.include_router(social_routes, prefix="/api/v1/social", tags=["Social Media Routes"])
app.include_router(whatsapp_routes, prefix="/api/v1/whatsapp", tags=["WhatsApp Meta Routes"])
app.include_router(instagram_routes, prefix="/api/v1/instagram", tags=["Instagram Business Login"])
# Shared Meta redirect_uri (WhatsApp ES + Instagram Business Login):
# https://useautobus.com/api/social/callback
app.include_router(meta_whatsapp_callback_routes, prefix="/api", tags=["WhatsApp Meta Callback"])
app.include_router(sms_sender_id_routes, prefix="/api/v1/sms-sender-ids", tags=["SMS Sender ID"])
app.include_router(chatwoot_routes, prefix="/api/v1/chatwoot", tags=["Chatwoot Routes"])
app.include_router(integration_routes, prefix="/api/v1/integrations", tags=["Integrations (self-test)"])
app.include_router(product_routes, prefix="/api/v1/products", tags=["Product Routes"])
app.include_router(order_routes, prefix="/api/v1/orders", tags=["Order Routes"])
app.include_router(intervention_routes, prefix="/api/v1/interventions", tags=["Interventions Routes"])
app.include_router(conversation_routes, prefix="/api/v1/conversations", tags=["Conversation Routes"])
app.include_router(intelligence_routes, prefix="/api/v1/intelligence", tags=["Intelligence / My AI"])
app.include_router(n8n_routes, prefix="/api/v1/admin/n8n", tags=["Platform Admin – n8n"])
app.include_router(
    workflow_action_routes,
    prefix="/api/v1/workflow-actions",
    tags=["Workflow actions (n8n)"],
)
app.include_router(admin_routes, prefix="/api/v1/admin", tags=["Platform Admin"])
app.include_router(ai_workflow_routes, prefix="/api/v1/admin/ai-workflows", tags=["AI Workflows"])
app.include_router(public_automation_routes, prefix="/api/v1", tags=["Automation Requests (Portal)"])
app.include_router(public_ads_routes, prefix="/api/v1/ads", tags=["Platform Ads"])
app.include_router(owner_resource_routes, prefix="/api/v1/resources", tags=["Owner Resources"])
app.include_router(portal_embed_routes, prefix="/api/v1/embed/portal", tags=["Embedded chat (portal)"])
app.include_router(embed_routes, prefix="/api/v1/embed", tags=["Embedded chat"])

# JWT Authentication Settings
class JWTSettings(BaseSettings):
    authjwt_secret_key: str = settings.SECRET_KEY
    authjwt_algorithm: str = settings.ALGORITHM
    authjwt_access_token_expires: int = settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60  # in seconds
    authjwt_refresh_token_expires: int = settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60  # in seconds


@AuthJWT.load_config
def get_config():
    return JWTSettings()