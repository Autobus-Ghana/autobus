from datetime import datetime, timedelta, timezone
from typing import Optional, List
from sqlalchemy.orm import Session
from core.subscription.model.subscription_plan import BillingPeriod, SubscriptionPlan
from core.subscription.model.user_subscription import UserSubscription, SubscriptionStatus
from core.user.model.User import User
import logging
import json

logger = logging.getLogger(__name__)


class SubscriptionService:
    def __init__(self, db: Session):
        self.db = db

    def get_all_plans(self) -> List[SubscriptionPlan]:
        """Get all active paid subscription plans (excludes the complimentary iOS Free plan)."""
        return (
            self.db.query(SubscriptionPlan)
            .filter(SubscriptionPlan.is_active == True, SubscriptionPlan.price > 0)
            .all()
        )

    def get_plan_by_id(self, plan_id: int) -> Optional[SubscriptionPlan]:
        """Get subscription plan by ID"""
        return self.db.query(SubscriptionPlan).filter(
            SubscriptionPlan.id == plan_id,
            SubscriptionPlan.is_active == True
        ).first()

    def get_user_active_subscription(self, user_id: str) -> Optional[UserSubscription]:
        """Get user's current active subscription"""
        return self.db.query(UserSubscription).filter(
            UserSubscription.user_id == user_id,
            UserSubscription.status == SubscriptionStatus.ACTIVE,
            UserSubscription.expires_at > datetime.now(timezone.utc)
        ).first()

    def get_user_subscription_history(self, user_id: str) -> List[UserSubscription]:
        """Get all user's subscription history"""
        return self.db.query(UserSubscription).filter(
            UserSubscription.user_id == user_id
        ).order_by(UserSubscription.created_at.desc()).all()

    def subscribe_user_by_phone(self, phone: str, plan_id: int, payment_reference: str = None) -> dict:
        """Subscribe user to a plan using phone number"""
        try:
            # Find user by phone number
            user = self.db.query(User).filter(User.phone == phone).first()
            if not user:
                return {
                    "success": False,
                    "message": "User not found with this phone number"
                }
            
            # Check if user account is enabled (verified)
            # if not user.enabled:
            #     return {
            #         "success": False,
            #         "message": "User account is not verified. Please verify your phone number first."
            #     }
            
            return self.subscribe_user(user.id, plan_id, payment_reference)
        
        except Exception as e:
            logger.error(f"Error subscribing user with phone {phone} to plan {plan_id}: {str(e)}")
            return {
                "success": False,
                "message": "Failed to subscribe user"
            }

    def subscribe_user(
        self,
        user_id: str,
        plan_id: int,
        payment_reference: str = None,
        expires_at: datetime = None,
        amount_paid: float = None,
        payment_provider: str = None,
        apple_original_transaction_id: str = None,
        apple_product_id: str = None,
    ) -> dict:
        """Subscribe user to a plan"""
        try:
            # Check if plan exists
            plan = self.get_plan_by_id(plan_id)
            if not plan:
                return {
                    "success": False,
                    "message": "Subscription plan not found"
                }

            # Check if user already has active subscription
            existing_subscription = self.get_user_active_subscription(user_id)
            if existing_subscription:
                return {
                    "success": False,
                    "message": "User already has an active subscription. Upgrade instead."
                }

            # Create new subscription (30 days from now unless the store provides expiry)
            if expires_at is None:
                expires_at = datetime.now(timezone.utc) + timedelta(days=30)
            
            new_subscription = UserSubscription(
                user_id=user_id,
                plan_id=plan_id,
                amount_paid=plan.price if amount_paid is None else amount_paid,
                expires_at=expires_at,
                payment_reference=payment_reference,
                payment_provider=payment_provider,
                apple_original_transaction_id=apple_original_transaction_id,
                apple_product_id=apple_product_id,
                status=SubscriptionStatus.ACTIVE
            )

            self.db.add(new_subscription)
            self.db.commit()
            self.db.refresh(new_subscription)

            try:
                from core.credits.service.credit_service import CreditService
                CreditService(self.db).initialize_credits_for_subscription(user_id, new_subscription)
            except Exception as e:
                logger.warning(f"Credit initialization failed for user {user_id}: {e}")

            # Initialize user agents based on any active subscription they may have
            try:
                init_res = self.initialize_user_agents_from_subscription(user_id)
                if init_res.get("success"):
                    logger.info(f"Initialized agents for user {user_id}")
                else:
                    logger.info(f"No agents initialized for user {user_id}: {init_res.get('message')}")
            except Exception as e:
                logger.error(f"Error initializing user agents after verification for {user_id}: {e}")

            logger.info(f"User {user_id} subscribed to plan {plan_id}")

            return {
                "success": True,
                "message": "Successfully subscribed to plan",
                "subscription_id": new_subscription.id,
                "plan_name": plan.name,
                "expires_at": expires_at.isoformat(),
                "amount_paid": new_subscription.amount_paid,
            }

        except Exception as e:
            logger.error(f"Error subscribing user {user_id} to plan {plan_id}: {str(e)}")
            self.db.rollback()
            return {
                "success": False,
                "message": "Failed to create subscription"
            }

    def apply_apple_subscription(
        self,
        user_id: str,
        plan_id: int,
        *,
        payment_reference: str,
        expires_at: datetime,
        amount_paid: float,
        apple_original_transaction_id: str,
        apple_product_id: str,
    ) -> dict:
        """Create or refresh a subscription from a verified App Store transaction."""
        plan = self.get_plan_by_id(plan_id)
        if not plan:
            return {"success": False, "message": "Subscription plan not found"}

        linked = (
            self.db.query(UserSubscription)
            .filter(
                UserSubscription.apple_original_transaction_id == apple_original_transaction_id
            )
            .order_by(UserSubscription.created_at.desc())
            .first()
        )
        if linked and linked.user_id != user_id:
            return {
                "success": False,
                "message": "This Apple subscription is already linked to another Autobus account",
            }

        now = datetime.now(timezone.utc)
        if linked:
            linked.plan_id = plan_id
            linked.status = SubscriptionStatus.ACTIVE
            linked.expires_at = expires_at
            linked.amount_paid = amount_paid
            linked.payment_reference = payment_reference
            linked.payment_provider = "apple_iap"
            linked.apple_product_id = apple_product_id
            linked.cancelled_at = None
            linked.updated_at = now
            self.db.commit()
            self.db.refresh(linked)
            try:
                from core.credits.service.credit_service import CreditService
                CreditService(self.db).initialize_credits_for_subscription(user_id, linked)
            except Exception as e:
                logger.warning(f"Credit initialization failed for Apple IAP user {user_id}: {e}")
            try:
                self.initialize_user_agents_from_subscription(user_id)
            except Exception as e:
                logger.error(f"Error initializing user agents after Apple IAP for {user_id}: {e}")
            return {
                "success": True,
                "message": "Apple subscription updated",
                "subscription_id": linked.id,
                "plan_name": plan.name,
                "expires_at": expires_at.isoformat(),
                "amount_paid": amount_paid,
            }

        existing = self.get_user_active_subscription(user_id)
        if existing:
            existing.status = SubscriptionStatus.CANCELLED
            existing.cancelled_at = now
            existing.updated_at = now
            existing.notes = ((existing.notes or "") + " | Replaced by Apple IAP").strip(" |")
            self.db.commit()

        return self.subscribe_user(
            user_id,
            plan_id,
            payment_reference,
            expires_at=expires_at,
            amount_paid=amount_paid,
            payment_provider="apple_iap",
            apple_original_transaction_id=apple_original_transaction_id,
            apple_product_id=apple_product_id,
        )

    def upgrade_subscription_by_phone(self, phone: str, new_plan_id: int, payment_reference: str = None) -> dict:
        """Upgrade user's subscription using phone number"""
        try:
            # Find user by phone number
            user = self.db.query(User).filter(User.phone == phone).first()
            if not user:
                return {
                    "success": False,
                    "message": "User not found with this phone number"
                }
            
            # Check if user account is enabled (verified)
            if not user.enabled:
                return {
                    "success": False,
                    "message": "User account is not verified. Please verify your phone number first."
                }
            
            return self.upgrade_subscription(user.id, new_plan_id, payment_reference)
        
        except Exception as e:
            logger.error(f"Error upgrading subscription for user with phone {phone}: {str(e)}")
            return {
                "success": False,
                "message": "Failed to upgrade subscription"
            }

    def upgrade_subscription(self, user_id: str, new_plan_id: int, payment_reference: str = None) -> dict:
        """Upgrade user's subscription to a higher plan"""
        try:
            # Check if new plan exists
            new_plan = self.get_plan_by_id(new_plan_id)
            if not new_plan:
                return {
                    "success": False,
                    "message": "New subscription plan not found"
                }

            # Get current active subscription
            current_subscription = self.get_user_active_subscription(user_id)
            if not current_subscription:
                return {
                    "success": False,
                    "message": "No active subscription found. Use subscribe instead."
                }

            # Check if it's actually an upgrade (price-wise)
            if new_plan.price <= current_subscription.plan.price:
                return {
                    "success": False,
                    "message": "New plan must be higher priced than current plan"
                }

            # Cancel current subscription
            current_subscription.status = SubscriptionStatus.CANCELLED
            current_subscription.cancelled_at = datetime.now(timezone.utc)
            current_subscription.updated_at = datetime.now(timezone.utc)

            # Create new subscription (extend remaining days + 30 days)
            remaining_days = max(0, current_subscription.days_remaining)
            expires_at = datetime.now(timezone.utc) + timedelta(days=30 + remaining_days)

            new_subscription = UserSubscription(
                user_id=user_id,
                plan_id=new_plan_id,
                amount_paid=new_plan.price,
                expires_at=expires_at,
                payment_reference=payment_reference,
                payment_provider="paystack" if payment_reference else None,
                status=SubscriptionStatus.ACTIVE,
                notes=f"Upgraded from plan {current_subscription.plan_id}"
            )

            self.db.add(new_subscription)
            self.db.commit()
            self.db.refresh(new_subscription)

            try:
                from core.credits.service.credit_service import CreditService
                CreditService(self.db).initialize_credits_for_subscription(user_id, new_subscription)
            except Exception as e:
                logger.warning(f"Credit initialization failed on upgrade for user {user_id}: {e}")

            logger.info(f"User {user_id} upgraded from plan {current_subscription.plan_id} to {new_plan_id}")

            return {
                "success": True,
                "message": "Successfully upgraded subscription",
                "subscription_id": new_subscription.id,
                "plan_name": new_plan.name,
                "expires_at": expires_at.isoformat(),
                "amount_paid": new_plan.price,
                "days_extended": remaining_days
            }

        except Exception as e:
            logger.error(f"Error upgrading subscription for user {user_id}: {str(e)}")
            self.db.rollback()
            return {
                "success": False,
                "message": "Failed to upgrade subscription"
            }

    def cancel_subscription_by_phone(self, phone: str, reason: str = None) -> dict:
        """Cancel user's subscription using phone number"""
        try:
            # Find user by phone number
            user = self.db.query(User).filter(User.phone == phone).first()
            if not user:
                return {
                    "success": False,
                    "message": "User not found with this phone number"
                }
            
            return self.cancel_subscription(user.id, reason)
        
        except Exception as e:
            logger.error(f"Error cancelling subscription for user with phone {phone}: {str(e)}")
            return {
                "success": False,
                "message": "Failed to cancel subscription"
            }

    def cancel_subscription(self, user_id: str, reason: str = None) -> dict:
        """Cancel user's active subscription"""
        try:
            subscription = self.get_user_active_subscription(user_id)
            if not subscription:
                return {
                    "success": False,
                    "message": "No active subscription found"
                }

            subscription.status = SubscriptionStatus.CANCELLED
            subscription.cancelled_at = datetime.now(timezone.utc)
            subscription.updated_at = datetime.now(timezone.utc)
            if reason:
                subscription.notes = f"Cancelled: {reason}"

            self.db.commit()

            logger.info(f"User {user_id} cancelled subscription {subscription.id}")

            return {
                "success": True,
                "message": "Subscription cancelled successfully",
                "subscription_id": subscription.id
            }

        except Exception as e:
            logger.error(f"Error cancelling subscription for user {user_id}: {str(e)}")
            self.db.rollback()
            return {
                "success": False,
                "message": "Failed to cancel subscription"
            }

    def create_subscription_plan(self, name: str, price: float, billing_period: str, billing_period_count: int, features: str, agents: str, description: str = None, is_active: bool = True) -> dict:
        """Create a new subscription plan"""
        try:
            # Check if plan name already exists
            existing_plan = self.db.query(SubscriptionPlan).filter(SubscriptionPlan.name == name).first()
            if existing_plan:
                return {
                    "success": False,
                    "message": f"Subscription plan with name '{name}' already exists"
                }
            
            # Create new plan
            new_plan = SubscriptionPlan(
                name=name,
                price=price,
                billing_period=billing_period,
                billing_period_count=billing_period_count,
                features=features,
                agents=agents,
                description=description,
                is_active=is_active,
                created_at=datetime.now(timezone.utc)
            )
            
            self.db.add(new_plan)
            self.db.commit()
            self.db.refresh(new_plan)
            
            logger.info(f"Created subscription plan: {new_plan.name} (ID: {new_plan.id})")
            
            return {
                "success": True,
                "message": "Subscription plan created successfully",
                "plan": new_plan
            }
            
        except Exception as e:
            logger.error(f"Error creating subscription plan '{name}': {str(e)}")
            self.db.rollback()
            return {
                "success": False,
                "message": "Failed to create subscription plan"
            }

    def check_user_has_feature_by_phone(self, phone: str, feature: str) -> dict:
        """Check if user has access to specific feature using phone number"""
        try:
            # Find user by phone number
            user = self.db.query(User).filter(User.phone == phone).first()
            if not user:
                return {
                    "success": False,
                    "message": "User not found with this phone number",
                    "has_access": False
                }
            
            has_access = self.check_user_has_feature(user.id, feature)
            return {
                "success": True,
                "phone": phone,
                "feature": feature,
                "has_access": has_access
            }
        
        except Exception as e:
            logger.error(f"Error checking feature access for user with phone {phone}: {str(e)}")
            return {
                "success": False,
                "message": "Failed to check feature access",
                "has_access": False
            }

    def check_user_has_feature(self, user_id: str, feature: str) -> bool:
        """Check if user's subscription includes a specific feature"""
        subscription = self.get_user_active_subscription(user_id)
        if not subscription:
            return False

        # Get features as list and check if feature is in the list
        features_list = subscription.plan.get_features_list()
        return feature.lower() in [f.lower() for f in features_list]

    @staticmethod
    def _is_paid_subscription(subscription: UserSubscription) -> bool:
        """True when the active plan is a paid Paystack/Apple plan, not the iOS free grant."""
        if not subscription or not subscription.is_active:
            return False
        provider = (subscription.payment_provider or "").strip().lower()
        if provider == "free":
            return False
        if provider in ("paystack", "apple_iap"):
            return True
        price = subscription.plan.price if subscription.plan else 0
        return (price or 0) > 0

    def _empty_subscription_status(self, status: str) -> dict:
        return {
            "has_active_subscription": False,
            "is_paid": False,
            "subscription_id": None,
            "plan_id": None,
            "plan_name": None,
            "plan_price": None,
            "features": None,
            "agents": None,
            "amount_paid": None,
            "expires_at": None,
            "days_remaining": 0,
            "status": status,
            "payment_provider": None,
            "apple_product_id": None,
        }

    def get_or_create_free_plan(self) -> SubscriptionPlan:
        """Find a zero-price plan, or create the iOS complimentary Free plan."""
        zero_price = (
            self.db.query(SubscriptionPlan)
            .filter(SubscriptionPlan.is_active == True, SubscriptionPlan.price <= 0)
            .order_by(SubscriptionPlan.id.asc())
            .first()
        )
        if zero_price:
            return zero_price

        named = (
            self.db.query(SubscriptionPlan)
            .filter(SubscriptionPlan.is_active == True)
            .filter(SubscriptionPlan.name.ilike("%free%"))
            .order_by(SubscriptionPlan.id.asc())
            .first()
        )
        if named:
            return named

        plan = SubscriptionPlan(
            name="Free",
            price=0,
            billing_period=BillingPeriod.MONTHLY,
            billing_period_count=1,
            features=json.dumps(["Order Management", "Emailing", "Customer Service"]),
            agents=json.dumps([]),
            description="Complimentary access for iOS while App Store subscriptions are under review.",
            is_active=True,
        )
        self.db.add(plan)
        self.db.commit()
        self.db.refresh(plan)
        logger.info(f"Created complimentary Free subscription plan (ID: {plan.id})")
        return plan

    def enroll_free_plan(self, user_id: str) -> dict:
        """Idempotently grant the complimentary Free plan (iOS / App Review)."""
        existing = self.get_user_active_subscription(user_id)
        if existing:
            return {
                "success": True,
                "message": "User already has an active subscription",
                "subscription_id": existing.id,
                "plan_name": existing.plan.name if existing.plan else None,
                "expires_at": existing.expires_at.isoformat() if existing.expires_at else None,
                "amount_paid": existing.amount_paid,
            }

        plan = self.get_or_create_free_plan()
        return self.subscribe_user(
            user_id,
            plan.id,
            payment_reference="ios_free_grant",
            expires_at=datetime.now(timezone.utc) + timedelta(days=365),
            amount_paid=0,
            payment_provider="free",
        )

    def get_user_subscription_status_by_phone(self, phone: str) -> dict:
        """Get user's subscription status using phone number"""
        try:
            # Find user by phone number
            user = self.db.query(User).filter(User.phone == phone).first()
            if not user:
                return self._empty_subscription_status("NO_USER")
            
            return self.get_user_subscription_status(user.id)
        
        except Exception as e:
            logger.error(f"Error getting subscription status for user with phone {phone}: {str(e)}")
            return self._empty_subscription_status("ERROR")

    def get_user_subscription_status(self, user_id: str) -> dict:
        """Get comprehensive subscription status for user"""
        subscription = self.get_user_active_subscription(user_id)
        
        if not subscription:
            return self._empty_subscription_status("NO_SUBSCRIPTION")

        return {
            "has_active_subscription": subscription.is_active,
            "is_paid": self._is_paid_subscription(subscription),
            "subscription_id": subscription.id,
            "plan_id": subscription.plan.id,
            "plan_name": subscription.plan.name,
            "plan_price": subscription.plan.price,
            "features": subscription.plan.get_features_list(),
            "agents": subscription.plan.get_agents_list(),
            "amount_paid": subscription.amount_paid,
            "expires_at": subscription.expires_at.isoformat(),
            "days_remaining": subscription.days_remaining,
            "status": subscription.status.value
            if hasattr(subscription.status, "value")
            else str(subscription.status),
            "payment_provider": subscription.payment_provider,
            "apple_product_id": subscription.apple_product_id,
        }


    def update_subscription_plan(self, plan_id: int, **updates) -> dict:
        """Update an existing subscription plan"""
        try:
            plan = self.db.query(SubscriptionPlan).filter(SubscriptionPlan.id == plan_id).first()
            if not plan:
                return {
                    "success": False,
                    "message": "Subscription plan not found"
                }

            # Update fields
            for field, value in updates.items():
                if value is not None and hasattr(plan, field):
                    setattr(plan, field, value)

            plan.updated_at = datetime.now(timezone.utc)
            self.db.commit()
            self.db.refresh(plan)

            logger.info(f"Updated subscription plan {plan_id}")

            return {
                "success": True,
                "message": "Subscription plan updated successfully",
                "plan": {
                    "id": plan.id,
                    "name": plan.name,
                    "price": plan.price,
                    "features": plan.get_features_list(),
                    "agents": plan.get_agents_list(),
                    "description": plan.description,
                    "is_active": plan.is_active
                }
            }

        except Exception as e:
            logger.error(f"Error updating subscription plan {plan_id}: {str(e)}")
            self.db.rollback()
            return {
                "success": False,
                "message": "Failed to update subscription plan"
            }

    def delete_subscription_plan(self, plan_id: int) -> dict:
        """Delete a subscription plan (soft delete by setting is_active=False)"""
        try:
            plan = self.db.query(SubscriptionPlan).filter(SubscriptionPlan.id == plan_id).first()
            if not plan:
                return {
                    "success": False,
                    "message": "Subscription plan not found"
                }

            # Check if any users are currently subscribed to this plan
            active_subscriptions = self.db.query(UserSubscription).filter(
                UserSubscription.plan_id == plan_id,
                UserSubscription.status == SubscriptionStatus.ACTIVE
            ).count()

            if active_subscriptions > 0:
                return {
                    "success": False,
                    "message": f"Cannot delete plan. {active_subscriptions} users are currently subscribed to this plan."
                }

            # Soft delete by setting is_active to False
            plan.is_active = False
            plan.updated_at = datetime.now(timezone.utc)
            self.db.commit()

            logger.info(f"Deleted subscription plan {plan_id}")

            return {
                "success": True,
                "message": "Subscription plan deleted successfully"
            }

        except Exception as e:
            logger.error(f"Error deleting subscription plan {plan_id}: {str(e)}")
            self.db.rollback()
            return {
                "success": False,
                "message": "Failed to delete subscription plan"
            }

    def get_all_plans_admin(self) -> List[SubscriptionPlan]:
        """Get all subscription plans (including inactive ones) - for admin use"""
        return self.db.query(SubscriptionPlan).all()

    def initialize_user_agents_from_subscription(self, user_id: str) -> dict:
        """Ensure the user's `agents` JSON is populated based on their active subscription plan.

        For each agent listed in the plan, create an entry in `User.agents` with
        expected keys derived from `core.agent.agent_params.AGENT_REQUIRED_PARAMS`.
        Existing agent entries will be merged and missing keys added rather than overwritten.
        """
        try:
            from core.agent.agent_params import AGENT_REQUIRED_PARAMS
            user = self.db.query(User).filter(User.id == user_id).first()
            if not user:
                return {"success": False, "message": "User not found"}

            subscription = self.get_user_active_subscription(user_id)
            if not subscription:
                return {"success": False, "message": "No active subscription found"}

            plan = subscription.plan
            agent_names = plan.get_agents_list()

            # Ensure user.agents is a dict
            user_agents = user.agents or {}
            
            # Track changes for logging/debugging
            changes_made = False

            for agent_name in agent_names:
                required = AGENT_REQUIRED_PARAMS.get(agent_name, [])
                
                existing = user_agents.get(agent_name)
                
                # Case 1: Agent doesn't exist yet - create new
                if existing is None:
                    params = {key: None for key in required}
                    agent_config = {
                        "params": params,
                        "status": "active"
                    }
                    user_agents[agent_name] = agent_config
                    changes_made = True
                    logger.info(f"Created new agent '{agent_name}' for user {user_id}")
                    continue
                
                # Case 2: Agent exists - update if needed
                if not isinstance(existing, dict):
                    # Invalid format, replace with new
                    params = {key: None for key in required}
                    agent_config = {
                        "params": params,
                        "status": "active"
                    }
                    user_agents[agent_name] = agent_config
                    changes_made = True
                    logger.warning(f"Replaced invalid agent format for '{agent_name}' for user {user_id}")
                    continue
                
                # Get existing params (handle different possible structures)
                existing_params = {}
                if "params" in existing and isinstance(existing["params"], dict):
                    # New format: params are nested
                    existing_params = existing["params"].copy()
                else:
                    # Old format: params might be at root level
                    # Copy all keys except reserved ones
                    reserved_keys = ["status", "params", "config", "id", "created_at", "updated_at"]
                    existing_params = {k: v for k, v in existing.items() 
                                    if k not in reserved_keys and not k.startswith('_')}
                
                # Check if we need to add any missing required params
                missing_keys = [key for key in required if key not in existing_params]
                
                if missing_keys:
                    # Add missing keys with None (preserve existing values)
                    for key in missing_keys:
                        existing_params[key] = None
                    
                    # Preserve status or use existing
                    status = existing.get("status", "active")
                    
                    # Update the agent config
                    user_agents[agent_name] = {
                        "params": existing_params,
                        "status": status
                    }
                    changes_made = True
                    logger.info(f"Added missing params {missing_keys} to agent '{agent_name}' for user {user_id}")
                
                # Optional: Check for params that are no longer required (cleanup)
                # Uncomment if you want to remove obsolete params
                """
                if "params" in existing and isinstance(existing["params"], dict):
                    current_params = set(existing["params"].keys())
                    required_params = set(required)
                    obsolete_params = current_params - required_params
                    
                    if obsolete_params:
                        # Remove obsolete params
                        for key in obsolete_params:
                            existing_params.pop(key, None)
                        
                        user_agents[agent_name] = {
                            "params": existing_params,
                            "status": existing.get("status", "inactive")
                        }
                        changes_made = True
                        logger.info(f"Removed obsolete params {list(obsolete_params)} from agent '{agent_name}' for user {user_id}")
                """

            # Check for agents that are no longer in the plan (optional)
            # This would remove agents that the user no longer has access to
            current_agents = set(user_agents.keys())
            allowed_agents = set(agent_names)
            agents_to_remove = current_agents - allowed_agents
            
            if agents_to_remove:
                for agent_name in agents_to_remove:
                    # Option 1: Remove completely
                    del user_agents[agent_name]
                    
                    # Option 2: Or just mark as inaccessible (commented out)
                    # if agent_name in user_agents:
                    #     user_agents[agent_name]["status"] = "unavailable"
                    
                    changes_made = True
                    logger.info(f"Removed agent '{agent_name}' (no longer in plan) for user {user_id}")

            # Only commit if changes were made
            if changes_made:
                user.agents = user_agents
                self.db.add(user)
                self.db.commit()
                self.db.refresh(user)
                message = "User agents updated successfully"
            else:
                message = "No updates needed - all agents are up to date"

            return {
                "success": True, 
                "message": message, 
                "agents": user.agents,
                "changes_made": changes_made
            }

        except Exception as e:
            logger.error(f"Error initializing user agents for {user_id}: {e}")
            self.db.rollback()
            return {"success": False, "message": str(e)}