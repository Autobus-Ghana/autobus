from typing import Dict, List, Any, Tuple
import logging
from core.nlu.config import INTENTS, MODEL, SYSTEM_PROMPTS
from core.nlu.service.llmclient import LLMClient  # Add this import
from utilities.phone_utils import extract_ghana_phone_numbers_from_text, clean_ocr_text

logger = logging.getLogger(__name__)

class IntentDetector:
    def __init__(self, use_advanced_model: bool = True):
        self.intents = INTENTS
        # Use advanced model for intent extraction if specified
        model = MODEL if use_advanced_model else None
        self.llm_client = LLMClient(model=model)

    def detect_intent_and_slots(
        self,
        user_message: str,
        conversation_history: List[Dict],
        current_intent: str = None,
        media_context: Dict = None,
        customer_session: bool = False,
    ) -> Tuple[str, Dict, List[str]]:
        """
        Detect user intent and extract slots from message
        Returns: (intent, extracted_slots, missing_slots)
        """
        
        # Prepare conversation context
        context = self._prepare_context(conversation_history)
        
        # Use transactional system prompt for intent detection
        system_prompt = SYSTEM_PROMPTS["transactional"].format(
            context=context, 
            missing_slots="",
            category="intent detection"
        )

        # Enhanced prompt with context awareness and precision
        prompt = self._create_enhanced_prompt(user_message, current_intent)
        
        # Create prompt for intent detection
        # prompt = f"""
        # Read the user's message and extract:
        # 1. The main intent from this list: {list(self.intents.keys())}
        # 2. Any relevant information (slots) for that intent
        
        # User message: "{user_message}"
        
        # Available intents and their slots:
        # {self._format_intents_for_prompt()}
        
        # IMPORTANT RULES FOR BENEFICIARY DETECTION:
        # - For send_money and buy_airtime intents: If the user mentions a NAME (not a phone number), extract it as "customer_name" slot
        # - Examples of names: "Send to John", "Buy airtime for Mom", "Send money to Ama"
        # - If a phone number is provided directly, use it as "recipient" or "phone_number" slot
        # - Both name and number can be provided; if name is provided, prefer extracting the name as customer_name slot
        # - The system will look up the saved customer by name and extract the phone number automatically

        # IMPORTANT RULE FOR REFERENCE EXTRACTION:
        # - Extract "for [purpose]" phrases as the reference slot, WITHOUT the "for" keyword
        # - Examples: "send 2 cedis to Autobusney for food" → extract reference as "food"
        # - Examples: "send 50 to John for transport" → extract reference as "transport"
        # - Examples: "send 100 cedis to Ama for school fees" → extract reference as "school fees"
        # - The reference describes the purpose or reason for the payment
        
        # TEMPORAL AND ACTION DISTINCTION:
        # - Past tense queries with "how much", "how many", "have I", "did I send", "have I sent" → expense_report
        # - Action language with "buy", "send", "pay" in imperative form → transactional (send_money, buy_airtime, pay_bill)
        # - Query language with "check", "view", "show", "tell me my" → expense_report or informational
        # - Time references like "today", "this week", "last month" in a query context → expense_report
        
        # Respond in this exact format:
        # INTENT: [detected_intent]
        # SLOTS: [json_object_with_slots]
        # MISSING: [comma_separated_missing_slots]
        
        # Example with customer name:
        # INTENT: send_money
        # SLOTS: {{"amount": "50", "customer_name": "John", "reference": "food"}}
        # MISSING: 
        
        # Example with direct phone number:
        # INTENT: send_money
        # SLOTS: {{"amount": "50", "recipient": "0234567890"}}
        # MISSING: reference
        # """

        try:
            logger.debug("Intent detection start: user_message=%s current_intent=%s media_present=%s", user_message, current_intent, bool(media_context))

            # Initialize extracted phones list
            extracted_phones_from_image = []
            
            # If audio bytes are present, transcribe and include transcription
            if media_context and media_context.get("audio_bytes"):
                try:
                    logger.info("Transcribing audio for intent detection: filename=%s", media_context.get("audio_filename"))
                    transcription = self.llm_client.transcribe_audio_from_bytes(
                        media_context.get("audio_bytes"),
                        filename=media_context.get("audio_filename", "audio.mp3")
                    )
                    logger.info("Audio transcription result: %s", transcription)
                    if transcription:
                        user_message = user_message + f"\n{transcription}"
                except Exception as ex:
                    logger.warning("Audio transcription failed: %s", ex)

            # If image is present, extract text and include in prompt (not as image parameter)
            extracted_phones_from_image = []
            photo_description = ""
            has_image = bool(
                media_context and (media_context.get("image_base64") or media_context.get("image_url"))
            )
            if has_image and customer_session:
                photo_description = self.llm_client.describe_product_image(
                    image_base64=media_context.get("image_base64"),
                    image_url=media_context.get("image_url"),
                    image_media_type=media_context.get("image_mime_type", "image/jpeg"),
                ) or ""
                if photo_description:
                    user_message = f"{user_message}\nPhoto shows this product: {photo_description}"
            if has_image and not photo_description:
                try:
                    logger.info("Extracting text from image for intent detection")
                    image_base64 = media_context.get("image_base64")
                    image_url = media_context.get("image_url")
                    image_media_type = media_context.get("image_mime_type", "image/jpeg")
                    
                    extracted_text = self.llm_client.extract_text_from_image(
                        image_base64=image_base64,
                        image_url=image_url,
                        image_media_type=image_media_type
                    )
                    logger.debug("Image text extraction result (raw): %s", extracted_text)
                    
                    if extracted_text:
                        # Clean OCR text to remove noise (e.g., debug output like "Autobus_backend  |")
                        clean_text = clean_ocr_text(extracted_text)
                        logger.debug("Image text after cleaning: %s", clean_text)
                        
                        # Extract Ghana phone numbers from the OCR text BEFORE adding to message
                        extracted_phones_from_image = extract_ghana_phone_numbers_from_text(clean_text)
                        if extracted_phones_from_image:
                            logger.info(f"[INTENT_DETECTION] Extracted phones from image: {extracted_phones_from_image}")
                        
                        # Add cleaned text to user message
                        user_message = user_message + f"\n{clean_text}"
                except Exception as ex:
                    logger.warning("Image text extraction failed: %s", ex)

            logger.info("Calling LLMClient for intent detection (model=%s)", self.llm_client.model)

            # Create prompt with extracted phone numbers from image
            prompt = self._create_enhanced_prompt(
                user_message,
                current_intent,
                extracted_phones_from_image,
                customer_session=customer_session,
            )

            response_text = self.llm_client.chat_completion(
                system_prompt=system_prompt,
                user_message=prompt,
                conversation_history=conversation_history,
                temperature=0.1,
                max_tokens=500,
                image_base64=(
                    media_context.get("image_base64")
                    if customer_session and media_context and not photo_description
                    else None
                ),
                image_url=(
                    media_context.get("image_url")
                    if customer_session and media_context and not photo_description
                    else None
                ),
                image_media_type=(media_context or {}).get("image_mime_type", "image/jpeg"),
            )

            logger.debug("Intent detection response text (truncated): %s", (response_text or '')[:1000])

            # Detect if model refused or reported inability to process images
            from core.product.service.image_match import is_vision_refusal

            if response_text and (
                is_vision_refusal(response_text) or "cannot_process_image" in response_text.lower()
            ):
                logger.info("Model reported it cannot process images; returning special intent")
                return "cannot_process_image", {}, []

            # Parse the LLM response
            intent, slots, missing_slots = self._parse_response(response_text)
            if photo_description:
                slots = dict(slots or {})
                slots["photo_description"] = photo_description
            
            # FALLBACK: If image phones were extracted but not found in slots, try to add them
            if extracted_phones_from_image and intent in ["send_money", "buy_airtime", "pay_bill"]:
                # Check if the user message references "this number" or similar but no phone was extracted
                user_msg_lower = user_message.lower()
                has_phone_reference = any(phrase in user_msg_lower for phrase in 
                    ["this number", "that number", "the number", "send to this", "send to that", 
                     "top up this", "buy for this", "send money to this"])
                
                phone_slots = ["recipient", "phone_number", "account_number"]
                has_empty_phone_slot = any(slot in missing_slots for slot in phone_slots)
                
                if has_phone_reference and has_empty_phone_slot and len(extracted_phones_from_image) > 0:
                    # Use the first extracted phone
                    first_phone = extracted_phones_from_image[0]
                    # Try to find the right slot name
                    if intent == "send_money" and "recipient" in phone_slots:
                        slots["recipient"] = first_phone
                        if "recipient" in missing_slots:
                            missing_slots.remove("recipient")
                    elif intent == "buy_airtime" and "phone_number" in phone_slots:
                        slots["phone_number"] = first_phone
                        if "phone_number" in missing_slots:
                            missing_slots.remove("phone_number")
                    elif intent == "pay_bill" and "account_number" in phone_slots:
                        slots["account_number"] = first_phone
                        if "account_number" in missing_slots:
                            missing_slots.remove("account_number")
                    
                    logger.info(f"[FALLBACK] Added extracted phone {first_phone} to slots for intent {intent}")
            
            return intent, slots, missing_slots
            
        except Exception as e:
            print(f"Error in intent detection: {e}")
            return "unknown", {}, []
    
    def _create_enhanced_prompt(
        self,
        user_message: str,
        current_intent: str = None,
        extracted_phones: List[str] = None,
        customer_session: bool = False,
    ) -> str:
        """Create enhanced prompt with context awareness and precision"""
        
        if extracted_phones is None:
            extracted_phones = []
        
        intent_guidelines = """
        INTENT DETECTION GUIDELINES:
        0. The moment a user send Hello, Hi, Hey or similar greeting, it should be classified as a greeting intent, regardless of any current intent. This is a clear signal of a new conversation flow.
        1. Be precise - read the exact words and phrasing in the user message
        2. If the message continues the current conversation flow, maintain the same intent
        3. Only change intent if the user clearly introduces a new topic or request
        4. For ambiguous messages, prefer the current intent if it makes contextual sense
        5. Consider conversation history when determining if this is a continuation

        HUMAN HANDOVER (INTERVENTIONS):
        - If the user asks to speak to a human / agent / support / representative, set intent to "request_intervention".
        - Examples: "talk to an agent", "human please", "I need support", "customer service", "can I speak to someone", "help me with an agent".
        - Do not classify "never mind" / "continue with the bot" as ending an intervention. Once a human agent is involved, the bot stays paused until the agent completes the conversation.

        CONVERSATION CLOSING:
        - If the user is clearly finished (bye, goodbye, that's all, nothing else), set intent to "goodbye".
        - If the user is only thanking the assistant or saying an answer helped (thanks, got it, that helps) without a new question, still prefer "goodbye" only when they also indicate they are done; otherwise keep the current conversational intent.
        
        CONTENT GENERATION (image / video / text):
        - If the user wants a picture, photo, poster, flyer, thumbnail, still, or visual artwork → generate_image
        - If the user wants a video, reel, clip, animation, or motion content → generate_video
        - If the user wants a caption, slogan, ad copy, hashtags, or written post text → generate_text
        - Examples: "make a flyer for my bakery" → generate_image; "15 second promo reel" → generate_video; "write a weekend sale caption" → generate_text
        - Follow-ups that refine the last output ("make it darker", "shorter", "add a logo", "more festive") KEEP the current generate_image / generate_video / generate_text intent
        - Only switch among generate_image, generate_video, and generate_text when the user clearly asks for a different medium
        
        CRITICAL RULES:
        - If user provides additional information for current intent: KEEP SAME INTENT
        - If user corrects or modifies previous information: KEEP SAME INTENT  
        - If user asks clarifying questions about current task: KEEP SAME INTENT
        - Only switch intent for completely new, unrelated user text
        
        TIME PERIOD EXTRACTION GUIDANCE:
        When extracting the "time_period" slot, use one of these standardized codes:
        - TODAY: for "today", "current day", "right now"
        - YESTERDAY: for "yesterday", "last day"
        - WEEK_1: for "this week", "current week"
        - WEEK_LAST: for "last week", "past week", "previous week"
        - WEEK_2: for "2 weeks", "14 days", "past 2 weeks"
        - MONTH_1: for "this month", "current month"
        - MONTH_LAST: for "last month", "past month", "previous month"
        - MONTH_3: for "last 3 months", "90 days", "past 3 months", "quarter"
        - MONTH_6: for "last 6 months", "180 days", "past 6 months"
        - YEAR_1: for "last year", "12 months", "this year", "annual", "yearly"
        - ALL_TIME: for "all time", "everything", "entire history", "since creation"
        
        IMPORTANT: Prefer the standardized codes above over natural language variations.
        If the user provides a time period that doesn't exactly match, convert it to the appropriate code.
        Examples:
        - "last 3 months" → "MONTH_3"
        - "for the past week" → "WEEK_LAST"
        - "this month" → "MONTH_1"
        - "all" → "ALL_TIME"
        - "over the last 6 months" → "MONTH_6"
        """
        
        current_intent_context = f"CURRENT_INTENT: {current_intent if current_intent else 'Intent Extraction'}"
        
        # Format extracted phones from images
        extracted_phones_info = ""
        if extracted_phones:
            extracted_phones_info = f"""
        IMPORTANT - PHONES EXTRACTED FROM IMAGE:
        The following Ghana phone number(s) were extracted from an image/document:
        {', '.join(extracted_phones)}

        If the user message mentions "this number", "that number", "the number", "send to this", etc., 
        these extracted phone numbers are the RECIPIENTS. Use them for "recipient" or "phone_number" slots.
        """
        
        customer_rules = ""
        if customer_session:
            customer_rules = """
        CUSTOMER SESSION (this person is shopping, not the business owner):
        - Use only: greeting, goodbye, business_conversation, view_products, view_product, create_order, check_order_status, request_intervention.
        - Never use merchant admin intents (add_product, update_product, send_money, expense_report, update_order, generate_image, and similar).
        - Questions about an order already placed (when will it arrive, did you create it, where is my order, I placed an order yesterday, I asked for 3) are check_order_status. They are never create_order. Do not put that sentence in item_name or product_name.
        - create_order only when they are buying now and give a product and/or a quantity.
        - If CURRENT_INTENT is create_order, keep it only when this message supplies a product name or a purchase quantity. Switch to check_order_status when they are talking about an order they already made.
        - item_name and product_name must be the product subject only. Never copy the whole message, a location ("I'm at Adenta", "Im at adenta"), or a delivery question ("can you deliver to my location") into those slots. If you cannot tell which product they mean, omit item_name and product_name.
        - "Can you deliver to my location" and similar delivery questions are business_conversation, unless they are about an order already placed (check_order_status).
        - "I'm at Adenta" / "Im at adenta" is a place. If CURRENT_INTENT is create_order, put the place in customer_location and leave item_name empty. Otherwise use business_conversation. It is never a product.
        - "Is this available", "do you have this", a size question, or a product photo is view_product. product_name must be the product, not the whole question.
        - "What products are available" or "can you share pictures" is view_products.
        - A line that starts with "Photo shows this product:" describes their picture. Use it as product_name when it matches something they could buy. Ignore watermarks and phone numbers in the photo.
        """

        return f"""
        {intent_guidelines}
        {customer_rules}
        
        {current_intent_context}
        You are an expert conversational AI that identifies user intent and extracts relevant slot information.
        A slot is a specific piece of information needed to fulfill an intent (e.g., amount, recipient).

        Your goals:
        1. Identify the user's **main intent** from the list below:
        List of defined intents: {list(self.intents.keys())}
        2. Extract slot values relevant to that intent.
        3. If the message is a continuation of an existing intent (current_intent = "{current_intent}"), 
        maintain that same intent **unless** the user clearly starts a new topic.
        4. Accurately identify missing required slots for that intent.
        
        User message to read: "{user_message}"
        {extracted_phones_info}
        Available intents and their slots:
        {self._format_intents_for_prompt()}
        
        DECISION PROCESS:
        - Is this a QUERY about PAST transactions? (how much, have I, did I send/bought) → expense_report
        - Is this an ACTION request? (buy, send, pay in command form) → transactional intent
        - Is this message clearly about a NEW intent? → Use new intent
        - Is this message continuing/refining the CURRENT intent? → Keep current intent
        - Is this message ambiguous but contextually related? → Prefer current intent
        
        Respond in this EXACT format:
        INTENT: [detected_intent]
        SLOTS: [json_object_with_slots]
        MISSING: [comma_separated_missing_slots]
        
        Examples:
        User starts send_money: "Send 50 cedis to 0234567890"
        INTENT: send_money
        SLOTS: {{"amount": "50", "recipient": "0234567890"}}
        MISSING: reference

        User queries expense report: "How much airtime have I sent to wifey today?"
        INTENT: expense_report
        SLOTS: {{"category": "airtime", "time_period": "TODAY"}}
        MISSING:

        User queries expense report: "How much money did I send this week?"
        INTENT: expense_report
        SLOTS: {{"category": "money_transfer", "time_period": "WEEK_1"}}
        MISSING:

        User starts bill payment: "Make bill payment of 1 cedi to 95200204493"
        INTENT: pay_bill
        SLOTS: {{"amount": "1", "account_number": "95200204493"}}
        MISSING: bill_type
        MISSING: bill_type

        User continues bill payment: "ECG, the card number is 95200204493"
        INTENT: pay_bill
        SLOTS: {{"bill_type": "ECG", "account_number": "95200204493"}}
        MISSING:

        User continues bill payment: "ECG, My account number is 95200204493 and I would like to send 1 cedi"
        INTENT: pay_bill
        SLOTS: {{"bill_type": "ECG", "account_number": "95200204493", "amount": "1"}}
        MISSING:

        User starts bill payment: "Pay my DStv bill, account 1234567890, amount is 50 cedis"
        INTENT: pay_bill
        SLOTS: {{"bill_type": "DStv", "account_number": "1234567890", "amount": "50"}}
        MISSING:

        User starts buy_airtime: "Buy me 5 cedis airtime to 0550748724"
        INTENT: buy_airtime
        SLOTS: {{"amount": "5", "phone_number": "0550748724"}}
        MISSING: network

        User queries expense: "How much airtime did I buy last month?"
        INTENT: expense_report
        SLOTS: {{"category": "airtime", "time_period": "MONTH_1"}}
        MISSING:

        User continues current intent: "Actually, make it 100 cedis instead"
        INTENT: send_money
        SLOTS: {{"amount": "100"}}
        MISSING: recipient,reference

        User starts new intent: "I want to check my balance"
        INTENT: check_balance
        SLOTS: {{}}
        MISSING:
        Examples end.

        Notes for accuracy:
        - Past tense verbs with query markers = expense_report
        - Imperative action verbs (buy, send, pay) = transactional
        - If the user's message clarifies or adds to the **current intent**, do not change it.
        - Only switch intent if the message explicitly refers to a different goal or action.
        - Always ensure `SLOTS` is valid JSON.
        """
    
    def _prepare_context(self, conversation_history: List[Dict]) -> str:
        """Prepare conversation context for the AI"""
        if not conversation_history:
            return "New conversation"
        
        context = "Recent conversation:\n"
        for msg in conversation_history[-5:]:  # Last 5 messages
            context += f"{msg['role']}: {msg['content']}\n"
        return context
    
    def _format_intents_for_prompt(self) -> str:
        """Format intents for the prompt"""
        formatted = ""
        for intent, details in self.intents.items():
            formatted += f"- {intent}: {details['description']} (slots: {', '.join(details['slots'])})\n"
        return formatted
    
    def _parse_response(self, response_text: str) -> Tuple[str, Dict, List[str]]:
        """Parse the AI response into structured data"""
        intent = "unknown"
        slots = {}
        missing_slots = []
        
        if not response_text:
            return intent, slots, missing_slots
            
        lines = response_text.strip().split('\n')
        for line in lines:
            if line.startswith('INTENT:'):
                intent = line.replace('INTENT:', '').strip()
            elif line.startswith('SLOTS:'):
                import json
                try:
                    slots_str = line.replace('SLOTS:', '').strip()
                    slots = json.loads(slots_str) if slots_str else {}
                except:
                    slots = {}
            elif line.startswith('MISSING:'):
                missing_str = line.replace('MISSING:', '').strip()
                missing_slots = [s.strip() for s in missing_str.split(',')] if missing_str else []
        
        return intent, slots, missing_slots