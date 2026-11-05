from typing import List, Optional, Set, Tuple
from sqlalchemy.orm import Session
from datetime import datetime
import logging
import re

from core.customers.model.customer import Customer, AccountType as CustomerAccountType
from core.customers.utility.network_detector import NetworkDetector, Network, AccountType
from core.user.model.User import User
from utilities.phone_utils import convert_to_local_ghana_format, normalize_ghana_phone_number

logger = logging.getLogger(__name__)


class CustomerService:
    """
    Service for managing user customers (saved payment recipients).
    Handles CRUD operations and network validation.
    """

    def __init__(self, db: Session):
        self.db = db

    def _normalize_phone_like(self, value: str) -> str:
        cleaned = "".join(ch for ch in (value or "") if ch.isdigit())
        if cleaned.startswith("233") and len(cleaned) > 3:
            cleaned = "0" + cleaned[3:]
        elif cleaned and not cleaned.startswith("0") and len(cleaned) == 9:
            cleaned = "0" + cleaned
        return cleaned

    def _resolve_user_db_id(self, user_identifier: str) -> Optional[str]:
        """
        Resolve a user identifier (db id, email, or phone) to the internal `users.id`.
        Customers uses a FK to `users.id`, so we must persist that value.
        """
        if not user_identifier:
            return None

        # 1) Direct match on primary key
        user = self.db.query(User).filter(User.id == user_identifier).first()
        if user:
            return user.id

        # 2) Email match (common JWT subject choice)
        user = self.db.query(User).filter(User.email == user_identifier).first()
        if user:
            return user.id

        # 3) Phone match (common WhatsApp/user_id choice)
        normalized_phone = self._normalize_phone_like(user_identifier)
        phone_candidates = {user_identifier}
        if normalized_phone:
            phone_candidates.add(normalized_phone)

        user = self.db.query(User).filter(User.phone.in_(list(phone_candidates))).first()
        if user:
            return user.id

        return None

    def _phone_match_key(self, value: Optional[str]) -> str:
        digits = re.sub(r"\D", "", value or "")
        if digits.startswith("233") and len(digits) >= 12:
            digits = digits[3:]
        elif digits.startswith("0") and len(digits) >= 10:
            digits = digits[1:]
        return digits[-9:] if len(digits) >= 9 else digits

    def _phone_candidates(self, value: Optional[str]) -> Set[str]:
        raw = (value or "").strip()
        candidates: Set[str] = set()
        if raw:
            candidates.add(raw)
        local = convert_to_local_ghana_format(raw) if raw else ""
        intl = normalize_ghana_phone_number(raw) if raw else ""
        if local:
            candidates.add(local)
        if intl:
            candidates.add(intl)
            if intl.startswith("233") and len(intl) == 12:
                candidates.add("0" + intl[3:])
        return {c for c in candidates if c}

    def find_customer_by_phone(self, user_id: str, phone: str) -> Optional[Customer]:
        """Find an active customer for this merchant by phone (local or international)."""
        resolved_user_id = self._resolve_user_db_id(user_id)
        if not resolved_user_id:
            return None

        candidates = self._phone_candidates(phone)
        if candidates:
            match = self.db.query(Customer).filter(
                Customer.user_id == resolved_user_id,
                Customer.is_active == True,
                Customer.customer_number.in_(list(candidates)),
            ).first()
            if match:
                return match

        key = self._phone_match_key(phone)
        if not key:
            return None

        for customer in self.get_customers(resolved_user_id):
            if self._phone_match_key(customer.customer_number) == key:
                return customer
        return None

    def add_customer(
        self,
        user_id: str,
        name: str,
        customer_number: str,
        network: Optional[str] = None,
        bank_code: Optional[str] = None,
        email: Optional[str] = None,
        allow_unknown_network: bool = False,
    ) -> Tuple[bool, Customer, str]:
        """
        Add a new customer for the user.

        Args:
            user_id: User ID
            name: Customer name
            customer_number: Mobile money wallet, bank account, or card number
            network: Network (MTN, VOD, AIR, BNK, MAS, VIS). Auto-detected if None
            bank_code: Bank code (required if network is BNK)

        Returns:
            Tuple of (success, customer_object, message)
        """
        try:
            resolved_user_id = self._resolve_user_db_id(user_id)
            if not resolved_user_id:
                return False, None, "User not found. Please log in again."

            name = (name or "").strip()[:100] or "Customer"
            customer_number = (customer_number or "").strip()
            if len(customer_number) > 20:
                digits_only = re.sub(r"\D", "", customer_number)
                customer_number = (digits_only or customer_number)[:20]
            if not customer_number:
                return False, None, "Customer number is required."
            logger.info(f"[BENEFICIARY_SERVICE] Adding customer for user: {resolved_user_id}, name: {name}")

            # If network not provided, try to detect it
            if not network:
                detected_network, network_msg = NetworkDetector.detect_network_from_phone(customer_number)
                if not detected_network:
                    if not allow_unknown_network:
                        return False, None, f"Could not auto-detect network: {network_msg}"
                    # Chat orders include non-Ghana and non-mobile numbers. Keep the
                    # contact anyway; OTH is not a mobile-money wallet.
                    network = "OTH"
                    logger.info(
                        "[BENEFICIARY_SERVICE] Saving contact without a mobile-money network: %s",
                        network_msg,
                    )
                else:
                    network = detected_network.value if hasattr(detected_network, "value") else str(detected_network)
                    logger.info(f"[BENEFICIARY_SERVICE] Auto-detected network: {network}")
            else:
                network = network.upper()

            network_enum = getattr(Network, network, None)
            if network != "OTH":
                # Validate network
                if not network_enum:
                    valid_networks = [
                        Network.MTN, Network.VOD, Network.AIR,
                        Network.BNK, Network.MAS, Network.VIS
                    ]
                    return False, None, f"Invalid network: {network}. Must be one of: {', '.join(valid_networks)}"

                # Validate customer number for this network
                is_valid, validation_msg = NetworkDetector.validate_customer_number(customer_number, network_enum)
                if not is_valid:
                    if not allow_unknown_network:
                        return False, None, f"Invalid customer number for {network}: {validation_msg}"
                    network = "OTH"
                    network_enum = None

            # If bank network, validate bank code
            if network_enum == Network.BNK:
                if not bank_code:
                    return False, None, "Bank code is required for bank account customers"
                is_valid, bank_msg = NetworkDetector.validate_bank_code(bank_code)
                if not is_valid:
                    return False, None, bank_msg
                bank_code = bank_code.upper()

            # Check for duplicates
            existing = self.db.query(Customer).filter(
                Customer.user_id == resolved_user_id,
                Customer.customer_number == customer_number,
                Customer.network == network,
                Customer.is_active == True
            ).first()

            if existing:
                return False, None, f"Customer '{existing.name}' with this {network} account already exists"

            # Determine account type and map to the DB enum values
            if network_enum is None:
                detected_account_type = AccountType.MOBILE_MONEY
            else:
                detected_account_type = NetworkDetector.determine_account_type(network_enum)
            if isinstance(detected_account_type, str):
                account_type_key = detected_account_type.lower()
            else:
                account_type_key = str(detected_account_type).lower()
            account_type = CustomerAccountType(account_type_key)

            # Create customer
            customer = Customer(
                user_id=resolved_user_id,
                name=name.strip(),
                email=email.strip() if email else None,
                customer_number=customer_number.strip(),
                network=network,
                bank_code=bank_code.upper() if bank_code else None,
                account_type=account_type
            )

            self.db.add(customer)
            self.db.commit()
            self.db.refresh(customer)

            logger.info(f"[BENEFICIARY_SERVICE] Customer added successfully: ID={customer.id}")
            return True, customer, f"Customer '{name}' ({network}: {customer_number}) saved successfully!"

        except Exception as e:
            self.db.rollback()
            logger.error(f"[BENEFICIARY_SERVICE] Error adding customer: {str(e)}", exc_info=True)
            return False, None, f"Error saving customer: {str(e)}"

    def get_customers(self, user_id: str) -> List[Customer]:
        """Get all active customers for a user."""
        try:
            resolved_user_id = self._resolve_user_db_id(user_id)
            if not resolved_user_id:
                return []

            logger.info(f"[BENEFICIARY_SERVICE] Fetching customers for user: {resolved_user_id}")
            customers = self.db.query(Customer).filter(
                Customer.user_id == resolved_user_id,
                Customer.is_active == True
            ).all()
            logger.info(f"[BENEFICIARY_SERVICE] Found {len(customers)} customers")
            return customers
        except Exception as e:
            logger.error(f"[BENEFICIARY_SERVICE] Error fetching customers: {str(e)}", exc_info=True)
            return []

    def get_customer(self, customer_id: int, user_id: str) -> Optional[Customer]:
        """Get a specific customer by ID (with user ownership check)."""
        try:
            resolved_user_id = self._resolve_user_db_id(user_id)
            if not resolved_user_id:
                return None

            logger.info(f"[BENEFICIARY_SERVICE] Fetching customer: {customer_id}")
            customer = self.db.query(Customer).filter(
                Customer.id == customer_id,
                Customer.user_id == resolved_user_id,
                Customer.is_active == True
            ).first()
            return customer
        except Exception as e:
            logger.error(f"[BENEFICIARY_SERVICE] Error fetching customer: {str(e)}", exc_info=True)
            return None

    def get_customers_by_ids(self, customer_ids: List[int], user_id: str) -> List[Customer]:
        """Fetch active customers by ID list, scoped to the authenticated user."""
        if not customer_ids:
            return []

        resolved_user_id = self._resolve_user_db_id(user_id)
        if not resolved_user_id:
            return []

        unique_ids = list(dict.fromkeys(customer_ids))
        customers = self.db.query(Customer).filter(
            Customer.id.in_(unique_ids),
            Customer.user_id == resolved_user_id,
            Customer.is_active == True,
        ).all()
        return customers

    def delete_customer(self, customer_id: int, user_id: str) -> Tuple[bool, str]:
        """Soft-delete a customer (mark as inactive)."""
        try:
            resolved_user_id = self._resolve_user_db_id(user_id)
            if not resolved_user_id:
                return False, "User not found. Please log in again."

            logger.info(f"[BENEFICIARY_SERVICE] Deleting customer: {customer_id}")
            customer = self.db.query(Customer).filter(
                Customer.id == customer_id,
                Customer.user_id == resolved_user_id
            ).first()

            if not customer:
                return False, "Customer not found"

            customer.is_active = False
            customer.updated_at = datetime.now()
            self.db.commit()

            logger.info(f"[BENEFICIARY_SERVICE] Customer deleted: {customer_id}")
            return True, f"Customer '{customer.name}' removed successfully"

        except Exception as e:
            self.db.rollback()
            logger.error(f"[BENEFICIARY_SERVICE] Error deleting customer: {str(e)}", exc_info=True)
            return False, f"Error removing customer: {str(e)}"

    def update_customer(
        self,
        customer_id: int,
        user_id: str,
        name: Optional[str] = None,
        customer_number: Optional[str] = None,
        bank_code: Optional[str] = None,
        email: Optional[str] = None,
        clear_email: bool = False,
    ) -> Tuple[bool, Customer, str]:
        """Update customer details."""
        try:
            resolved_user_id = self._resolve_user_db_id(user_id)
            if not resolved_user_id:
                return False, None, "User not found. Please log in again."

            logger.info(f"[BENEFICIARY_SERVICE] Updating customer: {customer_id}")
            customer = self.db.query(Customer).filter(
                Customer.id == customer_id,
                Customer.user_id == resolved_user_id
            ).first()

            if not customer:
                return False, None, "Customer not found"

            # Update name if provided
            if name:
                customer.name = name.strip()

            # Update customer number if provided
            if customer_number:
                network_enum = getattr(Network, customer.network, None)
                if not network_enum:
                    return False, None, f"Invalid stored network on customer: {customer.network}"
                is_valid, validation_msg = NetworkDetector.validate_customer_number(
                    customer_number,
                    network_enum
                )
                if not is_valid:
                    return False, None, f"Invalid customer number: {validation_msg}"
                customer.customer_number = customer_number.strip()

            # Update bank code if provided
            if bank_code and customer.network == "BNK":
                is_valid, bank_msg = NetworkDetector.validate_bank_code(bank_code)
                if not is_valid:
                    return False, None, bank_msg
                customer.bank_code = bank_code.upper()

            if clear_email:
                customer.email = None
            elif email is not None:
                customer.email = email.strip() if email else None

            customer.updated_at = datetime.now()
            self.db.commit()
            self.db.refresh(customer)

            logger.info(f"[BENEFICIARY_SERVICE] Customer updated: {customer_id}")
            return True, customer, "Customer updated successfully"

        except Exception as e:
            self.db.rollback()
            logger.error(f"[BENEFICIARY_SERVICE] Error updating customer: {str(e)}", exc_info=True)
            return False, None, f"Error updating customer: {str(e)}"

    def format_customer_list(self, customers: List[Customer]) -> str:
        """Format customer list for WhatsApp display."""
        if not customers:
            return "You have no saved customers."

        lines = ["Your customers:\n"]
        for idx, b in enumerate(customers, 1):
            account_display = f"{b.network}: {b.customer_number}"
            if b.bank_code:
                account_display += f" ({b.bank_code})"
            lines.append(f"{idx}. {b.name} - {account_display}")

        return "\n".join(lines)
