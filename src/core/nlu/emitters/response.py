from typing import Dict, Any

class ResponseFormatter:
    @staticmethod
    def format_response(intent: str, message_type: str, **kwargs) -> str:
        """Format responses in a friendly business assistant style"""
        
        if message_type == "missing_slots":
            return f"{kwargs.get('message', 'I would need a few more details to complete the task,')} {kwargs.get('prompt', '')}"
        
        elif message_type == "confirm_action":
            action_descriptions = {
                "send_money": f"send GHS {kwargs.get('amount')} to {kwargs.get('recipient')}",
                "buy_airtime": f"buy GHS {kwargs.get('amount')} airtime for {kwargs.get('phone_number')}",
                "buy_data": f"buy {kwargs.get('data_plan')} data for {kwargs.get('phone_number')}",
                "pay_bill": f"pay {kwargs.get('bill_type')} bill of GHS {kwargs.get('amount')}",
                "get_loan": f"apply for a GHS {kwargs.get('loan_amount')} loan"
            }
            action_desc = action_descriptions.get(intent, "complete this transaction")
            return f"🔒 Please enter your 5-digit PIN to confirm {action_desc}."
        
        elif message_type == "success":
            return f"✅ {kwargs.get('message', 'Action completed successfully!')}"
        
        elif message_type == "intent_not_clear":
            return "I'm not quite sure what you're asking. Could you please rephrase or provide more details? I can help you with: sending money, buying airtime, paying bills, tracking expenses, managing customers, or getting financial tips."
        
        elif message_type == "error":
            return kwargs.get("message") or "Something went wrong. Please try again."

        elif message_type == "ask_for_image_description":
            return "I couldn't process the image automatically. Could you please describe what's in the image, or send a short caption?"
        
        elif message_type == "invalid_pin":
            return "🔒 Invalid PIN. Please try again."

        elif message_type == "payment_confirmation":
            return kwargs.get('message', 'Please confirm this transaction.')

        elif message_type == "payment_cancelled":
            return "❌ Transaction cancelled. Your account has not been charged."

        elif message_type == "confirm_again":
            return f"❓ {kwargs.get('message', 'Please reply yes or no.')}"

        elif message_type == "processing":
            return kwargs.get('message', 'Your payment is being processed.')

        elif message_type == "transaction_in_progress":
            return "I am currently fulfilling a transaction for you. Please wait a moment, I will attend to you when I'm done."

        elif message_type == "intervention_created":
            return "please hold on i will get back to you in a minute"

        elif message_type == "intervention_active":
            return "A human agent is currently assisting you. Please share any extra details and they’ll respond shortly."

        else:
            return f"💬 {kwargs.get('message', '')}"