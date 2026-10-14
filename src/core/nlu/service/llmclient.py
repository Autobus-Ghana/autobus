import logging
from typing import Dict, List, Any, Optional
from core.nlu.config import (
    AUDIO_TRANSCRIPTION_MODEL,
    MODEL,
    openai_client,
)

logger = logging.getLogger(__name__)


class LLMClient:
    """Centralized LLM API client for handling all LLM conversations, including multimodal inputs"""
    
    def __init__(self, model: Optional[str] = None):
        self.client = openai_client()
        self.model = model or MODEL
    
    def chat_completion(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: Optional[List[Dict]] = None,
        temperature: float = 0.1,
        max_tokens: int = 500,
        image_url: Optional[str] = None,
        image_base64: Optional[str] = None,
        image_media_type: str = "image/jpeg"
    ) -> str:
        """
        Generic method for LLM chat completions with optional image support (vision)
        
        Args:
            system_prompt: The system prompt/instruction
            user_message: The current user message
            conversation_history: Previous conversation messages
            temperature: Creativity level (0-1)
            max_tokens: Maximum response length
            image_url: URL to an image (for vision capabilities)
            image_base64: Base64-encoded image data
            image_media_type: MIME type of the image (image/jpeg, image/png, image/gif, image/webp)
            
        Returns:
            LLM response as string
        """
        messages = self._build_messages(
            system_prompt,
            user_message,
            conversation_history,
            image_url=image_url,
            image_base64=image_base64,
            image_media_type=image_media_type,
        )

        try:
            logger.debug("Sending chat completion request: model=%s", self.model)
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_completion_tokens=max_tokens,
            )

            message = response.choices[0].message if response.choices else None
            content = getattr(message, "content", "") if message else ""
            return content.strip() if isinstance(content, str) else str(content).strip()

        except Exception as e:
            logger.error(f"Error in LLM API call: {e}")
            return ""
    
    def _build_messages(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: Optional[List[Dict]] = None,
        image_url: Optional[str] = None,
        image_base64: Optional[str] = None,
        image_media_type: str = "image/jpeg"
    ) -> List[Dict]:
        """Build messages for the OpenAI-compatible chat completions API."""
        messages: List[Dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        if conversation_history:
            for msg in conversation_history[-6:]:
                role = msg.get("role", "user")
                if role not in {"system", "assistant", "user"}:
                    role = "user"
                messages.append({"role": role, "content": msg.get("content", "")})
        
        if image_url or image_base64:
            user_content: List[Dict[str, Any]] = [
                {
                    "type": "text",
                    "text": user_message
                }
            ]

            if image_base64:
                data_url = f"data:{image_media_type};base64,{image_base64}"
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": data_url},
                })
            elif image_url:
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": image_url},
                })

            messages.append({"role": "user", "content": user_content})
            return messages

        messages.append({"role": "user", "content": user_message})
        return messages
    
    def structured_completion(
        self,
        system_prompt: str,
        user_message: str,
        expected_format: str,
        conversation_history: Optional[List[Dict]] = None
    ) -> str:
        """
        For structured outputs where you expect specific format
        
        Args:
            expected_format: Description of expected output format
        """
        enhanced_prompt = f"{user_message}\n\nPlease respond in this format:\n{expected_format}"
        
        return self.chat_completion(
            system_prompt=system_prompt,
            user_message=enhanced_prompt,
            conversation_history=conversation_history,
            temperature=0.1  # Lower temperature for structured outputs
        )
    
    def transcribe_audio(self, audio_file_path: str) -> Optional[str]:
        """
        Transcribe audio file to text using the configured transcription model
        
        Args:
            audio_file_path: Path to the audio file or file object
            
        Returns:
            Transcribed text or None if transcription fails
        """
        try:
            logger.info(f"Transcribing audio file: {audio_file_path}")
            
            with open(audio_file_path, "rb") as audio_file:
                transcript = self.client.audio.transcriptions.create(
                    model=AUDIO_TRANSCRIPTION_MODEL,
                    file=audio_file,
                    language="en"  # Specify English; adjust as needed for multilingual support
                )
            
            transcribed_text = transcript.text
            logger.info(f"Audio transcription successful: {transcribed_text}")
            return transcribed_text
            
        except Exception as e:
            logger.error(f"Error transcribing audio: {e}")
            return None
    
    def transcribe_audio_from_bytes(self, audio_bytes: bytes, filename: str = "audio.mp3") -> Optional[str]:
        """
        Transcribe audio from bytes using the configured transcription model
        
        Args:
            audio_bytes: Audio file content as bytes
            filename: Filename with extension (for format detection)
            
        Returns:
            Transcribed text or None if transcription fails
        """
        try:
            logger.info(f"Transcribing audio from bytes: {filename}")
            
            from io import BytesIO
            audio_file = BytesIO(audio_bytes)
            audio_file.name = filename
            
            transcript = self.client.audio.transcriptions.create(
                model=AUDIO_TRANSCRIPTION_MODEL,
                file=audio_file,
                language="en"  # Specify English; adjust as needed
            )
            
            transcribed_text = transcript.text
            logger.info(f"Audio transcription successful: {transcribed_text}")
            return transcribed_text
            
        except Exception as e:
            logger.error(f"Error transcribing audio from bytes: {e}")
            return None
    
    def extract_text_from_image(
        self,
        image_base64: Optional[str] = None,
        image_url: Optional[str] = None,
        image_media_type: str = "image/jpeg"
    ) -> Optional[str]:
        """
        Extract all text content from an image using vision capabilities
        
        Args:
            image_base64: Base64-encoded image data
            image_url: Direct URL to image
            image_media_type: MIME type of the image (image/jpeg, image/png, image/gif, image/webp)
            
        Returns:
            Extracted text from image or None if extraction fails
        """
        if not image_base64 and not image_url:
            logger.warning("extract_text_from_image: No image provided (both base64 and url are None)")
            return None
        
        try:
            logger.info("Extracting text from image")
            
            system_prompt = "You are an expert at extracting all visible text from images. Extract and return all text you can see in the image, preserving the structure and layout as much as possible."
            
            user_message = "Please extract and return all text visible in this image:"
            
            response = self.chat_completion(
                system_prompt=system_prompt,
                user_message=user_message,
                temperature=0.1,
                max_tokens=1000,
                image_base64=image_base64,
                image_url=image_url,
                image_media_type=image_media_type
            )
            
            logger.info(f"Text extraction successful. Extracted text length: {len(response) if response else 0}")
            return response if response else None
            
        except Exception as e:
            logger.error(f"Error extracting text from image: {e}")
            return None

    def describe_product_image(
        self,
        image_base64: Optional[str] = None,
        image_url: Optional[str] = None,
        image_media_type: str = "image/jpeg",
    ) -> Optional[str]:
        """Describe the product in a customer photo so it can be matched to the catalog."""
        if not image_base64 and not image_url:
            return None
        try:
            response = self.chat_completion(
                system_prompt=(
                    "You describe retail product photos for a shop assistant. "
                    "Reply with one short line: product type, color, material, and style. "
                    "Ignore shop watermarks, phone numbers, and captions overlaid on the photo. "
                    "If a code like AB-XXXX is printed on the image, put that code first. "
                    "If the image is not a product, reply exactly NOT_A_PRODUCT."
                ),
                user_message="What product is shown in this photo?",
                temperature=0.1,
                max_tokens=120,
                image_base64=image_base64,
                image_url=image_url,
                image_media_type=image_media_type,
            )
            text = (response or "").strip()
            if not text or "not_a_product" in text.lower():
                return None
            from core.product.service.image_match import is_vision_refusal

            if is_vision_refusal(text):
                logger.info("Product image description was a vision refusal; ignoring it")
                return None
            return text
        except Exception as e:
            logger.error(f"Error describing product image: {e}")
            return None
    
    def chat_completion_with_audio(
        self,
        system_prompt: str,
        user_message: str,
        conversation_history: Optional[List[Dict]] = None,
        temperature: float = 0.1,
        max_tokens: int = 500,
        audio_file_path: Optional[str] = None,
        audio_bytes: Optional[bytes] = None,
        audio_filename: str = "audio.mp3"
    ) -> str:
        """
        Process user message with optional audio transcription
        
        Args:
            system_prompt: The system prompt/instruction
            user_message: The current user message
            conversation_history: Previous conversation messages
            temperature: Creativity level (0-1)
            max_tokens: Maximum response length
            audio_file_path: Path to audio file to transcribe
            audio_bytes: Audio file content as bytes
            audio_filename: Filename for audio bytes (default: audio.mp3)
            
        Returns:
            LLM response as string
        """
        # Transcribe audio if provided
        audio_transcription = None
        if audio_file_path:
            audio_transcription = self.transcribe_audio(audio_file_path)
        elif audio_bytes:
            audio_transcription = self.transcribe_audio_from_bytes(audio_bytes, audio_filename)
        
        # Enhance user message with transcription
        enhanced_message = user_message
        if audio_transcription:
            enhanced_message = f"{user_message}\n\n[Audio transcription: {audio_transcription}]"
        
        return self.chat_completion(
            system_prompt=system_prompt,
            user_message=enhanced_message,
            conversation_history=conversation_history,
            temperature=temperature,
            max_tokens=max_tokens
        )

    def chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict]] = None,
        temperature: float = 0.2,
        max_tokens: int = 900,
    ) -> Any:
        """Chat completion that may return tool_calls. Falls back to plain text/JSON."""
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_completion_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        try:
            response = self.client.chat.completions.create(**kwargs)
            message = response.choices[0].message if response.choices else None
            if message is not None:
                return message
        except Exception as exc:
            logger.warning("Tool calling failed, falling back to JSON completion: %s", exc)

        fallback_messages = list(messages)
        fallback_messages.append(
            {
                "role": "system",
                "content": (
                    "Respond as JSON only, one object: "
                    '{"type":"reply","message":"..."} or '
                    '{"type":"ask_input","message":"...","kind":"text|image|video|file|choice","prompt":"..."} or '
                    '{"type":"confirm","message":"...","tool":"create_product","args":{}} or '
                    '{"type":"call_tool","tool":"list_products","args":{}}.'
                ),
            }
        )
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=fallback_messages,
                temperature=temperature,
                max_completion_tokens=max_tokens,
            )
            message = response.choices[0].message if response.choices else None
            if message is not None:
                return message
        except Exception as exc:
            logger.error("Fallback chat completion failed: %s", exc)

        class _Empty:
            content = ""
            tool_calls = None

        return _Empty()
