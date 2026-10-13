"""Auto-generated module that imports all SQLAlchemy model modules.

Importing these modules ensures their model classes are defined and
registered on `Base.metadata` so Alembic autogenerate can detect tables.

If any import fails, the exception is printed and import proceeds.
"""
from importlib import import_module
from utilities.dbconfig import Base

# List of model modules to import (module path relative to `src`)
_model_modules = [
	"core.user.model.User",
	"core.customers.model.customer",
	"core.cloudstorage.model.filemodel",
	"core.subscription.model.user_subscription",
	"core.subscription.model.subscription_plan",
	"core.credits.model.user_credit_balance",
	"core.credits.model.credit_usage_log",
	"core.credits.model.credit_purchase",
	"core.receipts.model.Receipt",
	"core.payments.model.timeline",
	"core.payments.model.paynetwork",
	"core.payments.model.paymentstatus",
	"core.payments.model.paymentmethod",
	"core.payments.model.payment",
	"core.payments.model.invoice",
	"core.payments.model.bill",
	"core.billing.model.billing_charge",
	"core.otp.model.otp",
	"core.notification.model.Notification",
	"core.nlu.model.Conversation",
	"core.interventions.model.Intervention",
	"core.histories.model.history",
	"core.auth.model.refreshtoken",
	"core.auth.model.password_reset_token",
	"core.paystack.model.transaction",
	"core.socialmedia.model.SocialAccount",
	"core.socialmedia.model.PostizOrganization",
	"core.socialmedia.model.DigitalMarketingPostAsset",
	"core.chatwoot.model.ChatwootAccount",
	"core.whatsapp.model.WhatsAppAccount",
	"core.instagram.model.InstagramAccount",
	"core.sms_sender_id.model.SmsSenderIdRegistration",
	"core.admin.model.platform_ad",
	"core.admin.model.owner_resource",
	"core.media.model.automedia",
	"core.product.model.product",
	"core.orders.model.order",
	"core.embed.model.embed",
	"core.ai_workflow.model.ai_workflow",
	"core.admin.model.automation_request",
]


for _m in _model_modules:
	try:
		import_module(_m)
	except Exception as _err:
		# Avoid crashing on import errors during alembic autogenerate; print for visibility
		print(f"dbmodels: failed to import {_m}: {_err}")
