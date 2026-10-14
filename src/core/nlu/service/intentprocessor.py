# core/nlu/service/intent_processor.py
import json
import threading
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Any, Optional
from core.customers.service.customer_service import CustomerService
from core.product.service.product_service import ProductService
from core.product.dto.product_create_dto import ProductCreateDTO
from core.product.dto.product_update_dto import ProductUpdateDTO
from core.product.dto.product_response_dto import ProductResponseDTO
from core.orders.order_messages import format_out_of_stock_notice
from core.orders.service.order_service import OrderService
from core.billing.service.order_invoice_service import OrderInvoiceService
from core.orders.dto.order_create_dto import OrderCreateDTO
from core.orders.dto.order_update_dto import OrderUpdateDTO
from core.nlu.service.llmclient import LLMClient
from core.nlu.config import SYSTEM_PROMPTS, RESPONSE_TEMPLATES, VENDOR_EXCLUSION_RULES
from core.nlu.service.slot_manager import is_placeholder_order_item_name
from core.nlu.service.customer_shop import (
    catalog_items_from_products,
    format_customer_catalog,
    format_customer_product,
    is_clear_product_subject,
    mentioned_size,
    resolve_catalog_query,
)
from core.nlu.service.datapipe.dataconfig import FINANCIAL_INSIGHTS_SYSTEM_PROMPT, INSIGHTS_SYSTEM_PROMPT
from core.nlu.service.datapipe.user_rag import UserRAGManager
from core.user.controller.usercontroller import get_db
from utilities.phone_utils import extract_ghana_phone_numbers_from_text, normalize_ghana_phone_number
from utilities.plain_text import strip_markdown_formatting
import logging
from core.nlu.service.datapipe.dataengine import EnhancedUserRAGManager

from core.agent.tools.agent_config.user_agent_config_service import AgentConfigService

logger = logging.getLogger(__name__)

class IntentProcessor:
    """Processes intents with NLU handlers (no LangChain agent loop)."""
    
    def __init__(self, db_session=None):
        self.llm_client = LLMClient()
        self.rag_manager = UserRAGManager()  # Initialize RAG manager
        self.db_session = db_session
        self._email_tool = None
        self._reply_images = threading.local()

    def begin_reply_images(self) -> None:
        self._reply_images.urls = []

    def take_reply_images(self) -> List[str]:
        urls = list(getattr(self._reply_images, "urls", None) or [])
        self._reply_images.urls = []
        return urls

    def _remember_reply_images(self, urls: List[str]) -> None:
        bucket = getattr(self._reply_images, "urls", None)
        if bucket is None:
            bucket = []
            self._reply_images.urls = bucket
        for url in urls:
            cleaned = (url or "").strip()
            if cleaned and cleaned not in bucket:
                bucket.append(cleaned)

    @property
    def email_tool(self):
        """Lazy-init: avoids Redis/DB setup unless an email intent runs."""
        if self._email_tool is None:
            from core.agent.tools.email.email import EmailTool

            self._email_tool = EmailTool()
        return self._email_tool
    
    def process_conversational_intent(
        self, 
        intent: str, 
        user_message: str, 
        conversation_history: List[Dict],
        slots: Dict[str, Any],
        user_id: str = None,
        user_data: Optional[Dict] = None,
        rag_context: Optional[str] = None,
    ) -> str:
        """
        Process conversational intents with optional vector-RAG context from the RAG API.

        Args:
            intent: Intent type
            user_message: User's message
            conversation_history: Conversation history
            slots: Extracted slots
            user_id: User ID (for logging / future personalization hooks)
            user_data: Additional user data
            rag_context: Pre-retrieved snippets (same-tenant Qdrant search), appended to the system prompt

        Returns:
            Generated response
        """
        # NOTE: pgvector was removed; optional Qdrant-backed context is supplied via `rag_context`.
        if intent == "greeting":
            return self._build_greeting_response(user_data)

        if intent == "goodbye" and self._is_customer_session(user_data):
            return RESPONSE_TEMPLATES["conversational"]["customer_goodbye"]

        prompt_key = (
            "customer_conversational"
            if self._is_customer_session(user_data)
            else "conversational"
        )

        # Prepare enhanced system prompt with user context
        system_prompt = self._build_enhanced_system_prompt(
            base_prompt=SYSTEM_PROMPTS[prompt_key],
            user_data=user_data,
            intent=intent,
            slots=slots
        )

        if rag_context and rag_context.strip():
            system_prompt = (
                system_prompt
                + "\n\n## Retrieved memory (same tenant — primary source for company/business facts)\n"
                + rag_context.strip()
                + "\n\nGrounding rules: Only use facts from Retrieved memory. "
                + "If a Product catalog section is present, that is the only source for product names, prices, and stock. "
                + "Never invent products, prices, or services, and never use website/document text as a product list."
            )
        else:
            system_prompt = (
                system_prompt
                + "\n\n## Retrieved memory\n"
                + "No relevant indexed documents were retrieved for this question. "
                + "Do not invent company or product facts. "
                + "If the user asks about products, say you do not have listed products yet. "
                + "If the user asks about the business, say you need more information "
                + "in the knowledge base (or that a human agent can help)."
            )

        if self._is_customer_session(user_data):
            temperature = 0.6
        else:
            temperature = 0.2 if intent == "business_conversation" else 0.7
        response = self.llm_client.chat_completion(
            system_prompt=system_prompt,
            user_message=user_message,
            conversation_history=conversation_history,
            temperature=temperature,
        )
        
        return strip_markdown_formatting(
            self._format_conversational_response(intent, response, slots)
        )
    
    def process_financial_tips_intent(
        self,
        intent: str,
        user_message: str,
        conversation_history: List[Dict],
        slots: Dict[str, Any],
        user_data: Optional[Dict] = None  # Add user_data parameter
    ) -> str:
        """
        Process financial tips with personalized user context
        """
        # Prepare enhanced system prompt with user context
        system_prompt = self._build_enhanced_system_prompt(
            base_prompt=SYSTEM_PROMPTS["financial_tips"],
            user_data=user_data,
            intent=intent,
            slots=slots
        )
        
        response = self.llm_client.chat_completion(
            system_prompt=system_prompt,
            user_message=user_message,
            conversation_history=conversation_history,
            temperature=0.4
        )
        
        return strip_markdown_formatting(
            self._format_financial_tips_response(intent, response, slots)
        )

    def process_expense_report_intent(
        self,
        intent: str,
        user_message: str,
        conversation_history: List[Dict],
        slots: Dict[str, Any],
        user_data: Optional[Dict] = None
    ) -> str:
        """
        Process expense report with enhanced financial insights
        """
        
        # Build enhanced system prompt
        system_prompt = self._build_enhanced_system_prompt(
            base_prompt=SYSTEM_PROMPTS["expense_report"],
            user_data=user_data,
            intent=intent,
            slots=slots
        )
        
        response = self.llm_client.chat_completion(
            system_prompt=system_prompt,
            user_message=user_message,
            conversation_history=conversation_history,
            temperature=0.4
        )
        
        return strip_markdown_formatting(response)
    
    def process_customers_intent(
    self,
    intent: str,
    user_message: str,
    conversation_history: List[Dict],
    slots: Dict[str, Any],
    user_data: Optional[Dict] = None
    ) -> tuple:
        """
        Process customers management using CustomerService.
        Returns (message, http_status) where http_status 200 means the intent was fulfilled.
        """

        db = next(get_db())

        customer_service = CustomerService(db)
        
        # For customer DB operations we need the internal `users.id` (FK target).
        user_id = (user_data or {}).get("db_user_id") or (user_data or {}).get("user_id") or "unknown"
        
        if intent == "add_customer":
            return self._handle_add_customer(customer_service, user_id, slots)
        elif intent == "view_customers":
            return self._handle_view_customers(customer_service, user_id)
        elif intent == "delete_customer":
            return self._handle_delete_customer(customer_service, user_id, slots)
        elif intent == "update_customer":
            return self._handle_update_customer(customer_service, user_id, slots)
        else:
            return ("Customer intent not supported", None)

    def _handle_add_customer(self, customer_service: CustomerService, user_id: str, slots: Dict) -> tuple:
        """Handle adding a new customer"""
        name = slots.get("customer_name")
        customer_number = slots.get("customer_number")
        network = slots.get("network")
        bank_code = slots.get("bank_code")

        # print user data
        print(f"[METHOD_HANDLE_ADD_BENEFICIARY] User Data for {user_id}")
        
        if not name or not customer_number:
            return ("Please provide both customer name and customer number to save a new customer.", None)
        
        success, customer, message = customer_service.add_customer(
            user_id=user_id,
            name=name,
            customer_number=customer_number,
            network=network,
            bank_code=bank_code
        )
        
        return (message, 200 if success else None)

    def _handle_view_customers(self, customer_service: CustomerService, user_id: str) -> tuple:
        """Handle viewing all customers"""
        customers = customer_service.get_customers(user_id)
        return (customer_service.format_customer_list(customers), None)

    def _handle_delete_customer(self, customer_service: CustomerService, user_id: str, slots: Dict) -> tuple:
        """Handle deleting a customer"""
        customer_name = slots.get("customer_name")
        
        if not customer_name:
            return ("Please specify which customer you want to remove.", None)
        
        customers = customer_service.get_customers(user_id)
        
        # Find customer by name
        target_customer = None
        for customer in customers:
            if customer.name.lower() == customer_name.lower():
                target_customer = customer
                break
        
        if not target_customer:
            return (f"Customer '{customer_name}' not found in your saved customers.", None)
        
        success, message = customer_service.delete_customer(target_customer.id, user_id)
        return (message, 200 if success else None)

    def _handle_update_customer(self, customer_service: CustomerService, user_id: str, slots: Dict) -> tuple:
        """Handle updating a customer"""
        customer_name = slots.get("customer_name")
        new_name = slots.get("new_customer_name")
        customer_number = slots.get("customer_number")
        bank_code = slots.get("bank_code")

        if not customer_name:
            return ("Please specify which customer you want to edit.", None)

        if not any([new_name, customer_number, bank_code]):
            return ("What would you like to update? You can send a new name, phone number, or bank code.", None)

        customers = customer_service.get_customers(user_id)
        target_customer = None
        for customer in customers:
            if customer.name.lower() == customer_name.lower():
                target_customer = customer
                break

        if not target_customer:
            return (f"Customer '{customer_name}' not found in your saved customers.", None)

        success, customer, message = customer_service.update_customer(
            customer_id=target_customer.id,
            user_id=user_id,
            name=new_name,
            customer_number=customer_number,
            bank_code=bank_code
        )
        return (message, 200 if success else None)

    def _build_enhanced_system_prompt(
        self,
        base_prompt: str,
        user_data: Optional[Dict],
        intent: str,
        slots: Dict
    ) -> str:
        """
        Build enhanced system prompt with user context RAG
        """
        conversational_intents = {
            "greeting",
            "normal_conversation",
            "business_conversation",
            "small_talk",
            "goodbye",
        }

        # Organization profile for chat (tenant-scoped, not platform vendor).
        user_context_section = ""
        if user_data and intent in conversational_intents:
            user_context_section = self._format_organization_context(user_data)
        elif user_data and intent == "expense_report":
            # user_data produced by NLU uses the key 'user_id' (not 'id')
            # Ensure we pass a string user_id to the RAG manager so it matches
            # the History.user_id column (which is stored as string).
            # Get user name
            user_name = f"{user_data.get('first_name', '')} {user_data.get('last_name', '')}".strip()
            if not user_name:
                user_name = user_data.get('username', 'User')
            
            # Get time frame from slots or default
            time_frame = slots.get('time_period', 'the selected period')
            
            # Fetch transactions using your existing method
            transactions = self.rag_manager.get_transaction_history(
                user_id=user_data.get('user_id'),
                intent=intent,
                slots=slots
            )
            
            rag_manager = EnhancedUserRAGManager()
            
            user_financial_context = rag_manager.get_financial_insights_context(
                user_name=user_name,
                user_id=user_data.get('user_id'),
                transactions=transactions,
                time_frame=time_frame,
                user_phone=user_data.get('phone_number')
            )
            user_context_section = f"User Transaction Data:\n{json.dumps(user_financial_context, indent=2)}"
            print(f"[ENHANCED_SYSTEM_PROMPT] User Transaction Data for {user_name}:\n{json.dumps(user_financial_context, indent=2)}")
        format_kwargs: Dict[str, Any] = {
            "context": user_context_section or "No organization profile on file.",
            "missing_slots": "",
            "category": slots.get("category", "general"),
        }
        if "{vendor_rules}" in base_prompt:
            format_kwargs["vendor_rules"] = VENDOR_EXCLUSION_RULES.strip()

        enhanced_prompt = base_prompt.format(**format_kwargs)
        from core.user.currency import currency_from_user_data, currency_prompt_rule

        enhanced_prompt = (
            f"{enhanced_prompt.rstrip()}\n\n{currency_prompt_rule(currency_from_user_data(user_data))}"
        )

        return enhanced_prompt

    @staticmethod
    def _format_organization_context(user_data: Dict[str, Any]) -> str:
        company = (user_data.get("company") or "").strip()
        workplace = (user_data.get("organization_workplace") or "").strip()
        fullname = (user_data.get("fullname") or "").strip()
        email = (user_data.get("email") or "").strip()
        lines = []
        if company:
            lines.append(f"Business name: {company}")
        if workplace:
            lines.append(f"Organization / workplace: {workplace}")
        if fullname:
            lines.append(f"Account holder: {fullname}")
        if email:
            lines.append(f"Contact email: {email}")
        from core.user.currency import currency_from_user_data, currency_label

        currency = currency_from_user_data(user_data)
        lines.append(f"Pricing currency: {currency_label(currency)}")
        if not lines:
            return "No organization profile on file."
        return "\n".join(lines)

    @staticmethod
    def _is_customer_session(user_data: Optional[Dict[str, Any]]) -> bool:
        return bool((user_data or {}).get("is_customer_session"))

    @staticmethod
    def _catalog_currency(user_data: Optional[Dict[str, Any]] = None) -> str:
        from core.user.currency import currency_from_user_data

        return currency_from_user_data(user_data)

    @staticmethod
    def _catalog_owner_id(user_id: str, user_data: Optional[Dict[str, Any]] = None) -> str:
        """Merchant users.id for catalog lookups (not the customer channel id)."""
        merchant = str(
            (user_data or {}).get("merchant_id")
            or (user_data or {}).get("db_user_id")
            or ""
        ).strip()
        if merchant:
            return merchant
        if user_id and ":" in user_id:
            prefix = user_id.split(":", 1)[0].strip()
            if prefix:
                return prefix
        return user_id

    @staticmethod
    def _business_display_name(user_data: Optional[Dict[str, Any]]) -> Optional[str]:
        if not user_data:
            return None
        for key in ("company", "organization_workplace", "fullname"):
            val = (user_data.get(key) or "").strip()
            if val:
                return val
        merchant_id = (user_data.get("merchant_id") or user_data.get("db_user_id") or "").strip()
        return merchant_id or None

    @staticmethod
    def _greeting_display_name(user_data: Optional[Dict[str, Any]]) -> Optional[str]:
        """Prefer fullname, then email local-part, for a short personalized greeting."""
        if not user_data:
            return None
        name = (user_data.get("fullname") or "").strip()
        if name:
            return name
        email = (user_data.get("email") or "").strip()
        if "@" in email:
            local = email.split("@", 1)[0].strip()
            if local:
                return local
        return None

    def _build_greeting_response(self, user_data: Optional[Dict[str, Any]]) -> str:
        templates = RESPONSE_TEMPLATES["conversational"]
        if self._is_customer_session(user_data):
            business = self._business_display_name(user_data)
            if business:
                return templates["customer_greeting_named"].replace("{business}", business)
            return templates["customer_greeting_anonymous"]
        display_name = self._greeting_display_name(user_data)
        if display_name:
            return templates["greeting_named"].replace("{name}", display_name)
        return templates["greeting_anonymous"]

    def _format_conversational_response(self, intent: str, response: str, slots: Dict) -> str:
        """Format conversational responses using templates"""
        template_data = RESPONSE_TEMPLATES["conversational"]
        
        if intent in template_data:
            template = template_data[intent]
            return template.format(response=response, **slots)
        
        return response

    def _format_financial_tips_response(self, intent: str, response: str, slots: Dict) -> str:
        """Format financial tips responses using templates"""
        template_data = RESPONSE_TEMPLATES["financial_tips"]
        
        if intent in template_data:
            template = template_data[intent]
            return template.format(response=response, **slots)
        
        return response

    # ===== EMAIL INTENT HANDLER =====
    def process_email_intent(
        self,
        intent: str,
        user_message: str,
        conversation_history: List[Dict],
        slots: Dict[str, Any],
        user_id: str,
        agent_name: str = "email_agent",
        user_data: Optional[Dict] = None
    ) -> str:
        """
        Process email intents using EmailTool
        
        Supported intents:
        - send_email: Send an email to a recipient
        - read_emails: Read recent emails from inbox
        - update_sender_email: Configure outbound sender address
        """
        try:
            if intent == "send_email":
                return self._handle_send_email(user_id, slots, agent_name)
            elif intent == "read_emails":
                return self._handle_read_emails(user_id, slots)
            elif intent == "update_sender_email":
                return self._handle_update_sender_email(user_id, slots, agent_name)
            else:
                return f"❌ Email intent '{intent}' not supported"
        except Exception as e:
            logger.error(f"Error processing email intent: {e}", exc_info=True)
            return f"❌ Error processing email: {str(e)[:100]}"

    def _handle_send_email(self, user_id: str, slots: Dict[str, Any], agent_name: str) -> str:
        """Handle send_email intent using EmailTool"""
        recipient_email = slots.get("recipient_email")
        subject = slots.get("subject")
        body = slots.get("body")
        
        if not recipient_email or not subject or not body:
            missing = []
            if not recipient_email:
                missing.append("recipient email")
            if not subject:
                missing.append("subject")
            if not body:
                missing.append("body")
            return f"❌ Missing required fields: {', '.join(missing)}"
        
        # Use EmailTool to send email
        result = self.email_tool._run(
            to_email=recipient_email,
            subject=subject,
            body=body,
            user_id=user_id,
            agent_name=agent_name
        )
        
        return result

    def _handle_read_emails(self, user_id: str, slots: Dict[str, Any]) -> str:
        """List emails this user sent via EmailTool (stored when send succeeds)."""
        raw_n = slots.get("num_emails", 10)
        try:
            limit = int(float(raw_n))
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(50, limit))

        rows = self.email_tool.list_sent_emails_for_user(user_id, limit=limit)
        if not rows:
            return (
                "📧 No sent emails on record yet. After you send mail through this assistant, "
                f"your last up to {limit} messages will appear here."
            )

        lines: List[str] = []
        for i, row in enumerate(rows, start=1):
            to_addr = row.get("to", "?")
            subj = row.get("subject", "(no subject)")
            sent = row.get("sent_at", "")
            lines.append(f"{i}. To: {to_addr} — {subj}\n   Sent: {sent}")
        return "📧 Your recent sent emails:\n" + "\n".join(lines)

    def _handle_update_sender_email(
        self, user_id: str, slots: Dict[str, Any], agent_name: str = "email_agent"
    ) -> str:
        """Persist the user's outbound sender email on their email_agent config."""
        sender_email = (slots.get("sender_email") or "").strip()
        if not sender_email:
            return "❌ Please provide the sender email address to use."

        from core.email.sender_email import SenderEmailInvalid, normalize_sender_email

        try:
            sender_email = normalize_sender_email(sender_email)
        except SenderEmailInvalid as exc:
            return f"❌ {exc}"

        db = next(get_db())
        try:
            service = AgentConfigService(db)
            result = service.create_or_update_agent(
                user_id=user_id,
                agent_name=agent_name,
                params={"sender_email": sender_email},
            )
            if not result.get("ok"):
                return f"❌ Could not update sender email: {result.get('message', 'unknown error')}"

            return (
                f"✅ Your sender email is now set to {sender_email}. "
                "Outgoing messages will use this address."
            )
        finally:
            db.close()

    # ===== PRODUCT MANAGEMENT INTENT HANDLER =====
    def process_product_management_intent(
        self,
        intent: str,
        user_message: str,
        conversation_history: List[Dict],
        slots: Dict[str, Any],
        user_id: str,
        user_data: Optional[Dict] = None
    ) -> str:
        """
        Process product management intents
        
        Supported intents:
        - add_product: Add a new product to inventory
        - update_product: Update product details
        - delete_product: Delete a product
        - view_products: View all products
        - view_product: View specific product details
        """
        try:
            if intent == "add_product":
                return self._handle_add_product(user_id, slots)
            elif intent == "update_product":
                return self._handle_update_product(user_id, slots)
            elif intent == "delete_product":
                return self._handle_delete_product(user_id, slots)
            elif intent == "view_products":
                return self._handle_view_products(
                    user_id, slots, user_data=user_data, user_message=user_message
                )
            elif intent == "view_product":
                return self._handle_view_product(
                    user_id, slots, user_data=user_data, user_message=user_message
                )
            else:
                return f"❌ Product management intent '{intent}' not supported"
        except Exception as e:
            logger.error(f"Error processing product management intent: {e}", exc_info=True)
            if self._is_customer_session(user_data):
                return "Sorry, I couldn't look that product up just now. Please try again in a moment."
            return f"❌ Error processing product: {str(e)[:100]}"

    def _resolved_photo_urls_from_slots(self, slots: Dict[str, Any]) -> List[str]:
        urls: List[str] = []
        photos = slots.get("photos")
        if isinstance(photos, list):
            urls.extend(str(url).strip() for url in photos if url and str(url).strip())
        elif isinstance(photos, str) and photos.strip():
            urls.append(photos.strip())
        photo = slots.get("photo")
        if photo and str(photo).strip():
            photo_str = str(photo).strip()
            if photo_str not in urls:
                urls.insert(0, photo_str)
        return urls

    def _format_product_photos(self, product, product_service: ProductService) -> str:
        product = product_service.get_product_by_id(str(product.product_id)) or product
        dto_photos = ProductResponseDTO.from_product(product).photos
        if not dto_photos:
            return ""
        if len(dto_photos) == 1:
            return f"Photo: {dto_photos[0]}"
        lines = [f"Photos ({len(dto_photos)}):"]
        lines.extend(f"  {index}. {url}" for index, url in enumerate(dto_photos, 1))
        return "\n".join(lines)

    def _handle_add_product(self, user_id: str, slots: Dict[str, Any]) -> str:
        """Handle add_product intent"""
        product_name = slots.get("product_name")
        price = slots.get("price")
        quantity = slots.get("quantity")
        
        if not product_name or not price or not quantity:
            missing = []
            if not product_name:
                missing.append("product name")
            if not price:
                missing.append("price")
            if not quantity:
                missing.append("quantity")
            return f"❌ Missing required fields: {', '.join(missing)}"

        db = next(get_db())
        product_service = ProductService(db)

        photo_urls = self._resolved_photo_urls_from_slots(slots) or [
            "https://placeholder.local/product.png"
        ]

        try:
            product_data = ProductCreateDTO(
                photos=photo_urls,
                name=product_name,
                description=slots.get("description"),
                price=self._to_float(price, default=0.0),
                category=slots.get("category"),
                condition=slots.get("condition") or "New",
                number_in_stock=self._to_int(quantity, default=0),
                link=slots.get("link"),
            )
        except Exception as e:
            return f"❌ Invalid product details: {str(e)}"

        success, product, message = product_service.create_product(product_data, user_id=user_id)
        if not success:
            return f"❌ {message}"
        if not product:
            return "❌ Product was created but could not be retrieved."

        photos_block = self._format_product_photos(product, product_service)
        photos_suffix = f"\n{photos_block}" if photos_block else ""
        return (
            f"✅ {message}\n"
            f"Product: {product.name}\n"
            f"Inventory ID: {product.inventory_id}\n"
            f"Price: {product.price}\n"
            f"Stock: {product.number_in_stock if product.number_in_stock is not None else 'N/A'}"
            f"{photos_suffix}"
        )

    def _handle_update_product(self, user_id: str, slots: Dict[str, Any]) -> str:
        """Handle update_product intent"""
        product_id = slots.get("product_id")

        db = next(get_db())
        product_service = ProductService(db)

        product = self._find_product(product_service, slots, user_id=user_id)
        if not product:
            if product_id:
                return f"❌ Product '{product_id}' not found"
            return "❌ Product ID, inventory ID, or product name is required for updating"

        update_payload = {}
        if slots.get("product_name") is not None:
            update_payload["name"] = slots.get("product_name")
        if slots.get("price") is not None:
            update_payload["price"] = self._to_float(slots.get("price"))
        if slots.get("quantity") is not None:
            update_payload["number_in_stock"] = self._to_int(slots.get("quantity"))
        if slots.get("condition") is not None:
            update_payload["condition"] = slots.get("condition")
        if slots.get("category") is not None:
            update_payload["category"] = slots.get("category")
        if slots.get("description") is not None:
            update_payload["description"] = slots.get("description")
        photo_urls = self._resolved_photo_urls_from_slots(slots)
        if len(photo_urls) > 1:
            update_payload["photos"] = photo_urls
        elif len(photo_urls) == 1:
            update_payload["photo"] = photo_urls[0]
        if slots.get("link") is not None:
            update_payload["link"] = slots.get("link")

        if not update_payload:
            return "❌ No updates specified"

        try:
            update_data = ProductUpdateDTO(**update_payload)
        except Exception as e:
            return f"❌ Invalid update fields: {str(e)}"

        success, updated_product, message = product_service.update_product(str(product.product_id), update_data)
        if not success:
            return f"❌ {message}"
        if not updated_product:
            return "❌ Product was updated but could not be retrieved."

        photos_block = self._format_product_photos(updated_product, product_service)
        photos_suffix = f"\n{photos_block}" if photos_block else ""
        return (
            f"✅ {message}\n"
            f"Product: {updated_product.name}\n"
            f"Inventory ID: {updated_product.inventory_id}"
            f"{photos_suffix}"
        )

    def _handle_delete_product(self, user_id: str, slots: Dict[str, Any]) -> str:
        """Handle delete_product intent"""
        product_id = slots.get("product_id")

        db = next(get_db())
        product_service = ProductService(db)

        product = self._find_product(product_service, slots, user_id=user_id)
        if not product:
            if product_id:
                return f"❌ Product '{product_id}' not found"
            return "❌ Product ID, inventory ID, or product name is required for deletion"

        success, message = product_service.delete_product(str(product.product_id))
        if not success:
            return f"❌ {message}"

        return f"✅ {message} (Removed: {product.name} - {product.inventory_id})"

    def _handle_view_products(
        self,
        user_id: str,
        slots: Dict[str, Any],
        user_data: Optional[Dict] = None,
        user_message: str = "",
    ) -> str:
        """Handle view_products intent"""
        db = next(get_db())
        product_service = ProductService(db)

        category = slots.get("category")
        owner_id = self._catalog_owner_id(user_id, user_data)
        products = product_service.get_products_by_user(owner_id, category=category)
        if self._is_customer_session(user_data):
            related_reply = self._related_image_reply(
                slots, product_service, owner_id, user_data, user_message
            )
            if related_reply:
                return related_reply
            products = self._owned_products(
                product_service, owner_id, user_id, limit=50, category=category
            )
            self._queue_product_images(products, product_service, per_product=1, total_limit=4)
            heading = str((slots or {}).get("catalog_heading") or "").strip() or None
            listing = format_customer_catalog(
                catalog_items_from_products(products),
                heading=heading,
                currency=self._catalog_currency(user_data),
            )
            return self._append_size_note(listing, user_message, products)
        if not products:
            return "📦 No products found in your inventory yet."

        lines = ["📦 Your Products:"]
        for index, product in enumerate(products[:20], 1):
            image_count = len(product_service.list_product_images(str(product.product_id)))
            if not image_count and product.photo:
                image_count = 1
            photo_suffix = f" | Photos: {image_count}" if image_count else ""
            lines.append(
                f"{index}. {product.name} | ID: {product.product_id} | "
                f"Inventory: {product.inventory_id} | "
                f"Price: {self._catalog_currency(user_data)} {product.price} | "
                f"Stock: {product.number_in_stock if product.number_in_stock is not None else 'N/A'}"
                f"{photo_suffix}"
            )

        if len(products) > 20:
            lines.append(f"...and {len(products) - 20} more products.")
        return "\n".join(lines)

    def _handle_view_product(
        self,
        user_id: str,
        slots: Dict[str, Any],
        user_data: Optional[Dict] = None,
        user_message: str = "",
    ) -> str:
        """Handle view_product intent"""
        product_id = slots.get("product_id")
        product_name = slots.get("product_name")

        db = next(get_db())
        product_service = ProductService(db)
        is_customer = self._is_customer_session(user_data)
        owner_id = self._catalog_owner_id(user_id, user_data)

        product = None
        unclear_subject = bool(
            is_customer
            and product_name
            and not product_id
            and not is_clear_product_subject(str(product_name))
        )
        if unclear_subject:
            owned = self._owned_products(product_service, owner_id, user_id, limit=50)
            self._queue_product_images(owned, product_service, per_product=1, total_limit=4)
            listing = format_customer_catalog(
                catalog_items_from_products(owned),
                currency=self._catalog_currency(user_data),
            )
            return f"Which one are you asking about?\n\n{listing}"
        product = self._find_product(product_service, slots, user_id=owner_id)
        if not product and product_name:
            matches = product_service.find_products_for_user(str(product_name), owner_id)
            if not matches:
                catalog = catalog_items_from_products(
                    product_service.get_products_by_user(owner_id, skip=0, limit=100)
                )
                resolved = resolve_catalog_query(str(product_name), catalog)
                if resolved:
                    ids = {item.product_id for item in resolved}
                    matches = [
                        item
                        for item in product_service.get_products_by_user(
                            owner_id, skip=0, limit=100
                        )
                        if str(item.product_id) in ids
                    ]
            if len(matches) > 1:
                heading = "I found a few matches. Which one did you mean?"
                return format_customer_catalog(
                    catalog_items_from_products(matches),
                    heading=heading,
                    currency=self._catalog_currency(user_data),
                ) if is_customer else (
                    heading
                    + "\n"
                    + "\n".join(
                        f"- {item.name} (ID: {item.product_id})" for item in matches
                    )
                )
            if len(matches) == 1:
                product = matches[0]

        if not product:
            label = product_name or product_id or "that product"
            if is_customer:
                owned = self._owned_products(product_service, owner_id, user_id, limit=50)
                self._queue_product_images(owned, product_service, per_product=1, total_limit=4)
                listing = format_customer_catalog(
                    catalog_items_from_products(owned),
                    currency=self._catalog_currency(user_data),
                )
                return self._append_size_note(
                    f'I don\'t have "{label}" on the shelf right now.\n\n{listing}',
                    user_message,
                    owned,
                )
            if product_id:
                return f"❌ Product '{product_id}' not found"
            return "❌ Product ID, inventory ID, or product name is required"

        if is_customer:
            items = catalog_items_from_products([product])
            currency = self._catalog_currency(user_data)
            self._queue_product_images([product], product_service, per_product=3, total_limit=3)
            body = (
                format_customer_product(items[0], currency=currency)
                if items
                else format_customer_catalog([], currency=currency)
            )
            return self._append_size_note(body, user_message, [product])

        photos_block = self._format_product_photos(product, product_service)
        photos_line = photos_block or f"Photo: {product.photo or 'N/A'}"
        return (
            "📦 Product Details:\n"
            f"ID: {product.product_id}\n"
            f"Inventory ID: {product.inventory_id}\n"
            f"Name: {product.name}\n"
            f"Price: {self._catalog_currency(user_data)} {product.price}\n"
            f"Quantity: {product.number_in_stock if product.number_in_stock is not None else 'N/A'}\n"
            f"Category: {product.category or 'N/A'}\n"
            f"Condition: {product.condition}\n"
            f"Description: {product.description or 'N/A'}\n"
            f"{photos_line}\n"
            f"Link: {product.link or 'N/A'}"
        )

    # ===== ORDER MANAGEMENT INTENT HANDLER =====
    def process_order_management_intent(
        self,
        intent: str,
        user_message: str,
        conversation_history: List[Dict],
        slots: Dict[str, Any],
        user_id: str,
        user_data: Optional[Dict] = None
    ) -> str:
        """
        Process order management intents
        
        Supported intents:
        - create_order: Create a new order
        - update_order: Update order details
        - send_order_invoice: Paystack invoice to customer chat
        """
        try:
            if intent == "create_order":
                return self._handle_create_order(user_id, slots, user_data=user_data)
            elif intent == "update_order":
                return self._handle_update_order(user_id, slots)
            elif intent == "send_order_invoice":
                return self._handle_send_order_invoice(user_id, slots, user_data)
            elif intent == "check_order_status":
                return self._handle_check_order_status(user_id, user_message, user_data)
            else:
                return f"❌ Order management intent '{intent}' not supported"
        except Exception as e:
            logger.error(f"Error processing order management intent: {e}", exc_info=True)
            if self._is_customer_session(user_data):
                return "Sorry, I couldn't check that order just now. Please try again in a moment."
            return f"❌ Error processing order: {str(e)[:100]}"

    def _handle_create_order(
        self,
        user_id: str,
        slots: Dict[str, Any],
        user_data: Optional[Dict] = None,
    ) -> str:
        """Handle create_order intent"""
        item_name = slots.get("item_name")
        quantity = slots.get("quantity")
        is_customer = self._is_customer_session(user_data)

        if (
            not item_name
            or not quantity
            or is_placeholder_order_item_name(str(item_name))
        ):
            missing = []
            if not item_name or is_placeholder_order_item_name(str(item_name)):
                missing.append("item name")
            if not quantity:
                missing.append("quantity")
            return f"❌ Missing required fields: {', '.join(missing)}"

        raw_phone = str(slots.get("customer_phone") or "").strip()
        if not raw_phone and user_data:
            raw_phone = str(user_data.get("customer_phone") or "").strip()
        if not raw_phone and user_id and ":" not in str(user_id):
            raw_phone = str(user_id).strip()
        customer_phone = normalize_ghana_phone_number(raw_phone) if raw_phone else "N/A"

        customer_name = (str(slots.get("customer_name") or "").strip())
        if not customer_name:
            customer_name = f"Customer ({customer_phone})"

        db = next(get_db())
        order_service = OrderService(db)
        product_service = ProductService(db)
        owner_id = self._catalog_owner_id(user_id, user_data)

        line_quantity = self._to_int(quantity, default=0)
        if line_quantity <= 0:
            return "❌ Quantity must be greater than 0."

        from core.embed.scope import for_sub_business, sub_business_from_session

        sub_business_id = sub_business_from_session(user_id or "")

        def _owned(limit: int = 100):
            return for_sub_business(
                product_service.get_products_by_user(owner_id, skip=0, limit=limit),
                sub_business_id,
            )

        if is_customer and not is_clear_product_subject(str(item_name)):
            listing = format_customer_catalog(
                catalog_items_from_products(_owned(50)),
                currency=self._catalog_currency(user_data),
            )
            return f"Which one would you like?\n\n{listing}"

        matches = []
        product_id = slots.get("product_id")
        if product_id:
            by_id = product_service.get_product_by_id(str(product_id))
            if by_id:
                matches = [by_id]
        if not matches:
            matches = for_sub_business(
                product_service.find_products_for_user(str(item_name), owner_id),
                sub_business_id,
            )
        if not matches:
            owned = _owned(100)
            catalog = catalog_items_from_products(owned)
            resolved = resolve_catalog_query(str(item_name), catalog)
            if resolved:
                ids = {item.product_id for item in resolved}
                matches = [item for item in owned if str(item.product_id) in ids]
        matched_product = matches[0] if len(matches) == 1 else None
        if len(matches) > 1:
            heading = "Which product did you mean?"
            if is_customer:
                return format_customer_catalog(
                    catalog_items_from_products(matches),
                    heading=heading,
                    currency=self._catalog_currency(user_data),
                )
            return heading + "\n" + "\n".join(
                f"- {item.name}" for item in matches
            )

        if is_customer:
            if not matched_product:
                listing = format_customer_catalog(
                    catalog_items_from_products(_owned(50)),
                    currency=self._catalog_currency(user_data),
                )
                return (
                    f'I don\'t have "{item_name}" on the shelf right now.\n\n'
                    f"{listing}"
                )
            stock = getattr(matched_product, "number_in_stock", None)
            if stock is not None and int(stock) <= 0:
                listing = format_customer_catalog(
                    catalog_items_from_products(_owned(50)),
                    currency=self._catalog_currency(user_data),
                )
                return (
                    f"{matched_product.name} just sold out. Here's what we still have:\n\n{listing}"
                )
            item_name = matched_product.name

        unit_price = self._to_decimal(slots.get("unit_price"), default=Decimal("0"))
        if matched_product is not None and getattr(matched_product, "price", None) is not None:
            unit_price = self._to_decimal(matched_product.price, default=unit_price)
        subtotal = self._to_decimal(
            slots.get("subtotal_amount"), default=(unit_price * line_quantity)
        )

        from core.embed.turn_context import embed_turn, record_embed_action

        turn = embed_turn()
        metadata = dict(slots.get("custom_metadata") or {})
        if turn:
            metadata["channel"] = "embed"
            metadata["conversation_id"] = turn.get("conversation_id") or ""
            metadata["external_customer_id"] = turn.get("external_customer_id") or ""
            if turn.get("sub_business_id"):
                metadata["sub_business_id"] = turn.get("sub_business_id")
                metadata["sub_business_name"] = turn.get("sub_business_name") or ""
            if matched_product is not None and getattr(matched_product, "external_id", None):
                metadata["external_id"] = matched_product.external_id

        try:
            order_data = OrderCreateDTO(
                customer_name=customer_name,
                customer_phone=customer_phone,
                customer_email=slots.get("customer_email"),
                customer_location=slots.get("customer_location"),
                order_type=(slots.get("order_type") or "sale").lower(),
                item_name=item_name,
                quantity=line_quantity,
                order_source=slots.get("order_source") or "chat",
                subtotal_amount=subtotal,
                discount_amount=self._to_decimal(slots.get("discount_amount"), default=Decimal("0")),
                tax_amount=self._to_decimal(slots.get("tax_amount"), default=Decimal("0")),
                shipping_amount=self._to_decimal(slots.get("shipping_amount"), default=Decimal("0")),
                currency_code=(
                    slots.get("currency_code") or self._catalog_currency(user_data)
                ).upper(),
                payment_method=slots.get("payment_method"),
                payment_reference=slots.get("payment_reference"),
                payment_details=slots.get("payment_details"),
                notes=slots.get("notes"),
                tags=slots.get("tags"),
                custom_metadata=metadata or slots.get("custom_metadata"),
            )
        except Exception as e:
            return f"❌ Invalid order details: {str(e)}"

        seller_user_id = user_id
        if matched_product and getattr(matched_product, "user_id", None):
            seller_user_id = matched_product.user_id
        elif user_data:
            merchant = str(
                user_data.get("merchant_id") or user_data.get("db_user_id") or ""
            ).strip()
            if merchant:
                seller_user_id = merchant

        success, order, message = order_service.create_order(
            order_data, user_id=seller_user_id
        )
        if not success:
            return f"❌ {message}"
        if not order:
            return "❌ Order was created but could not be retrieved."

        if is_customer:
            price_bit = ""
            if unit_price and unit_price > 0:
                price_bit = f" at {self._catalog_currency(user_data)} {unit_price} each"
            if turn:
                record_embed_action(
                    {
                        "type": "order.created",
                        "order": {
                            "order_id": str(order.order_id),
                            "order_number": order.order_number,
                            "source": "embed",
                            "external_customer_id": metadata.get("external_customer_id") or "",
                            "conversation_id": metadata.get("conversation_id") or "",
                            "sub_business": {
                                "external_id": metadata.get("sub_business_id") or "",
                                "name": metadata.get("sub_business_name") or "",
                            }
                            if metadata.get("sub_business_id")
                            else None,
                            "items": [
                                {
                                    "external_id": metadata.get("external_id") or "",
                                    "name": item_name,
                                    "quantity": line_quantity,
                                    "unit_price": str(unit_price),
                                }
                            ],
                            "total": str(order.total_amount),
                            "currency": order.currency_code,
                            "status": "pending",
                            "payment_status": "pending",
                            "fulfillment_status": "unfulfilled",
                        },
                    }
                )
            return (
                f"Done, I've put {line_quantity} {item_name} aside for you{price_bit}. "
                f"Your order number is {order.order_number}, "
                f"total {order.total_amount} {order.currency_code}. "
                f"We'll confirm the rest with you shortly."
            )

        response_lines = [
            f"✅ {message}",
            f"Order Number: {order.order_number}",
            f"Customer: {order.customer_name}",
            f"Total Amount: {order.total_amount} {order.currency_code}",
        ]
        metadata = order.custom_metadata if isinstance(order.custom_metadata, dict) else {}
        if metadata.get("requires_cs_followup"):
            response_lines.append("")
            response_lines.append(format_out_of_stock_notice(str(item_name)))
        return "\n".join(response_lines)

    def _handle_send_order_invoice(
        self,
        user_id: str,
        slots: Dict[str, Any],
        user_data: Optional[Dict[str, Any]] = None,
    ) -> str:
        order_id = slots.get("order_id")
        order_number = slots.get("order_number")

        if not order_id and not order_number:
            return (
                "❌ Please provide the order number (e.g. ORD-20260318-12345) "
                "or order ID so I can send the invoice."
            )

        db = next(get_db())
        invoice_service = OrderInvoiceService(db)
        created_by = None
        if user_data and user_data.get("db_user_id"):
            created_by = str(user_data["db_user_id"])

        success, message = invoice_service.send_invoice_for_order(
            merchant_user_id=user_id,
            order_id=str(order_id).strip() if order_id else None,
            order_number=str(order_number).strip() if order_number else None,
            customer_email=(slots.get("customer_email") or "").strip() or None,
            created_by_user_id=created_by,
        )
        return message

    def _handle_update_order(self, user_id: str, slots: Dict[str, Any]) -> str:
        """Handle update_order intent"""
        order_id = slots.get("order_id")

        if not order_id:
            return "❌ Order ID is required for updating"

        db = next(get_db())
        order_service = OrderService(db)

        update_payload = {}
        for field in [
            "order_status", "payment_status", "fulfillment_status", "payment_method",
            "payment_reference", "payment_details", "customer_name", "customer_phone",
            "customer_email", "customer_location", "notes", "tags", "custom_metadata"
        ]:
            if slots.get(field) is not None:
                update_payload[field] = slots.get(field)

        if slots.get("subtotal_amount") is not None:
            update_payload["subtotal_amount"] = self._to_decimal(slots.get("subtotal_amount"))
        if slots.get("discount_amount") is not None:
            update_payload["discount_amount"] = self._to_decimal(slots.get("discount_amount"))
        if slots.get("tax_amount") is not None:
            update_payload["tax_amount"] = self._to_decimal(slots.get("tax_amount"))
        if slots.get("shipping_amount") is not None:
            update_payload["shipping_amount"] = self._to_decimal(slots.get("shipping_amount"))

        if slots.get("quantity") is not None or slots.get("item_name") is not None:
            item_qty = self._to_int(slots.get("quantity"), default=None)
            if item_qty is not None and item_qty <= 0:
                return "❌ Quantity must be greater than 0."

            if slots.get("item_name") is not None:
                update_payload["item_name"] = slots.get("item_name")

            if item_qty is not None:
                update_payload["quantity"] = item_qty

        if not update_payload:
            return "❌ No updates specified"

        try:
            update_data = OrderUpdateDTO(**update_payload)
        except Exception as e:
            return f"❌ Invalid order update details: {str(e)}"

        success, order, message = order_service.update_order(order_id, update_data)
        if not success:
            return f"❌ {message}"
        if not order:
            return "❌ Order was updated but could not be retrieved."

        return (
            f"✅ {message}\n"
            f"Order Number: {order.order_number}\n"
            f"Status: {order.order_status} | Payment: {order.payment_status} | Fulfillment: {order.fulfillment_status}"
        )

    def _owned_products(self, product_service, owner_id: str, user_id: str, limit: int = 100, category: Optional[str] = None):
        from core.embed.scope import for_sub_business, sub_business_from_session
        from core.embed.turn_context import embed_turn

        products = product_service.get_products_by_user(
            owner_id, category=category, skip=0, limit=limit
        )
        sub_business_id = sub_business_from_session(user_id or "")
        if not sub_business_id:
            turn = embed_turn() or {}
            sub_business_id = str(turn.get("sub_business_id") or "")
        return for_sub_business(products, sub_business_id)

    def _public_product_image_urls(self, product, product_service: ProductService, limit: int = 1) -> List[str]:
        from core.cloudstorage.service.storageservice import refresh_public_object_url

        raw: List[str] = []
        photo = getattr(product, "photo", None)
        if photo:
            raw.append(str(photo))
        try:
            for image in product_service.list_product_images(str(product.product_id)):
                url = getattr(image, "url", None)
                if url:
                    raw.append(str(url))
        except Exception as exc:
            logger.warning("Could not list product images: %s", exc)
        urls: List[str] = []
        for item in raw:
            url = refresh_public_object_url(item)
            if not url or "placeholder" in url.lower():
                continue
            if url not in urls:
                urls.append(url)
            if len(urls) >= limit:
                break
        return urls

    def _queue_product_images(self, products, product_service: ProductService, per_product: int = 1, total_limit: int = 4) -> None:
        urls: List[str] = []
        for product in products or []:
            urls.extend(self._public_product_image_urls(product, product_service, limit=per_product))
            if len(urls) >= total_limit:
                break
        self._remember_reply_images(urls[:total_limit])

    def _related_image_reply(
        self,
        slots: Dict[str, Any],
        product_service: ProductService,
        owner_id: str,
        user_data: Optional[Dict],
        user_message: str,
    ) -> Optional[str]:
        """Reply with the products a customer photo looked like."""
        raw = str((slots or {}).get("related_product_ids") or "").strip()
        if not raw:
            return None
        resolved_owner = product_service._resolve_user_db_id(owner_id) or owner_id
        picked = []
        for product_id in raw.split(","):
            product_id = product_id.strip()
            if not product_id:
                continue
            product = product_service.get_product_by_id(product_id)
            if not product or not product.is_active:
                continue
            if product.user_id and resolved_owner and product.user_id != resolved_owner:
                continue
            picked.append(product)
        if not picked:
            return None
        self._queue_product_images(picked, product_service, per_product=1, total_limit=4)
        heading = str(slots.get("image_match_heading") or "").strip() or (
            "These look closest to the photo you sent:"
        )
        listing = format_customer_catalog(
            catalog_items_from_products(picked),
            heading=heading,
            currency=self._catalog_currency(user_data),
        )
        return self._append_size_note(listing, user_message, picked)

    def _append_size_note(self, text: str, user_message: str, products) -> str:
        size = mentioned_size(user_message)
        if not size:
            return text
        blob = " ".join(
            " ".join(
                [
                    str(getattr(product, "name", "") or ""),
                    str(getattr(product, "description", "") or ""),
                    str(getattr(product, "category", "") or ""),
                ]
            )
            for product in products or []
        ).lower()
        if size.lower() in blob:
            return text
        return (
            f"{text}\n\nI can't confirm size {size} from what we have listed. "
            "I'll have the shop check that. I can still hold one while they confirm, if you want."
        )

    @staticmethod
    def _phone_variants(phone: str) -> List[str]:
        import re

        raw = re.sub(r"\D", "", phone or "")
        variants = {str(phone or "").strip(), raw}
        try:
            normalized = normalize_ghana_phone_number(phone)
        except Exception:
            normalized = raw
        if normalized:
            variants.add(normalized)
            if normalized.startswith("233") and len(normalized) >= 12:
                variants.add("0" + normalized[3:])
                variants.add("+" + normalized)
        return [item for item in variants if item]

    def _handle_check_order_status(
        self,
        user_id: str,
        user_message: str,
        user_data: Optional[Dict] = None,
    ) -> str:
        """Tell a customer the status of an order already placed. Never invent a delivery date."""
        if not self._is_customer_session(user_data):
            return "Open your orders list to check status."

        stated = extract_ghana_phone_numbers_from_text(user_message or "")
        phone = stated[0] if stated else str((user_data or {}).get("customer_phone") or "").strip()
        if not phone:
            return (
                "I can look up an order from the phone number it was placed with. "
                "Send that phone number and I will check."
            )

        from sqlalchemy import desc
        from core.orders.model.order import Order

        db = next(get_db())
        owner_id = self._catalog_owner_id(user_id, user_data)
        orders = (
            db.query(Order)
            .filter(Order.user_id == owner_id, Order.customer_phone.in_(self._phone_variants(phone)))
            .order_by(desc(Order.order_date))
            .limit(3)
            .all()
        )
        if not orders:
            return (
                "I could not find an order for this number. "
                "If you used a different phone, send that number and I will check again."
            )

        lines = ["Here is the latest I have for your order:"]
        for order in orders:
            status = order.order_status.value if hasattr(order.order_status, "value") else order.order_status
            payment = order.payment_status.value if hasattr(order.payment_status, "value") else order.payment_status
            fulfillment = (
                order.fulfillment_status.value
                if hasattr(order.fulfillment_status, "value")
                else order.fulfillment_status
            )
            items = order.order_items if isinstance(order.order_items, list) else []
            bits = []
            for item in items[:3]:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("item_name") or "item"
                    qty = item.get("quantity")
                    bits.append(f"{qty} x {name}" if qty else str(name))
            summary = ", ".join(bits) if bits else "items on file"
            lines.append(
                f"{order.order_number}: {summary}. Status: {status}. Payment: {payment}. Fulfillment: {fulfillment}."
            )
            if getattr(order, "delivery_date", None):
                lines.append(f"Delivery date on file: {order.delivery_date:%d %b %Y}.")
            else:
                lines.append("There is no delivery date on file yet. The shop will confirm when it is ready.")
        return "\n".join(lines)

    def _find_product(self, product_service: ProductService, slots: Dict[str, Any], user_id: Optional[str] = None):
        """Resolve a product from supported slot keys, scoped to the merchant when possible."""
        product_id = slots.get("product_id")
        product = None
        if product_id:
            product = product_service.get_product_by_id(str(product_id))
            if not product:
                product = product_service.get_product_by_inventory_id(str(product_id))

        if product is None:
            inventory_id = slots.get("inventory_id")
            if inventory_id:
                product = product_service.get_product_by_inventory_id(str(inventory_id))

        if product is None:
            product_name = slots.get("product_name")
            if product_name and user_id:
                product = product_service.find_product_for_user(str(product_name), user_id)
            elif product_name:
                products = product_service.get_product_by_name(str(product_name), limit=1)
                product = products[0] if products else None

        if product and user_id:
            resolved = product_service._resolve_user_db_id(user_id)
            if resolved and product.user_id and product.user_id != resolved:
                return None
        return product

    def _to_int(self, value: Any, default: Optional[int] = None) -> Optional[int]:
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _to_float(self, value: Any, default: Optional[float] = None) -> Optional[float]:
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _to_decimal(self, value: Any, default: Optional[Decimal] = None) -> Optional[Decimal]:
        if value is None:
            return default
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return default



