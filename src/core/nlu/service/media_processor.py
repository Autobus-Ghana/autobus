import os
import requests
import logging
import base64
import mimetypes
from typing import Optional, Dict, Tuple
from io import BytesIO

logger = logging.getLogger(__name__)

# Instagram's lookaside CDN refuses downloads that do not look like a browser.
_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/jpeg,image/png,image/*,*/*;q=0.8",
}


class MediaProcessor:
    """Service for processing media files from WhatsApp (images, audio)"""
    
    def __init__(self):
        self.meta_api_key = os.getenv("META_API_KEY")
        # Use the Facebook Graph API (WhatsApp Cloud API is exposed under the
        # Facebook Graph endpoints). Allow overriding via env var.
        self.base_url = os.getenv("WHATSAPP_GRAPH_BASE_URL", "https://graph.facebook.com/v17.0")
        
        # Supported media types
        self.supported_images = {"image/jpeg", "image/png", "image/gif", "image/webp"}
        self.supported_audio = {"audio/aac", "audio/mp4", "audio/mpeg", "audio/ogg"}
    
    def download_media_from_whatsapp(
        self,
        media_id: str
    ) -> Optional[Tuple[bytes, str, str]]:
        """
        Download media from WhatsApp Cloud API
        
        Args:
            media_id: The media ID from the WhatsApp message
            
        Returns:
            Tuple of (media_bytes, mime_type, filename) or None if download fails
        """
        try:
            logger.info(f"Downloading media with ID: {media_id}")
            
            # Step 1: Get media URL from WhatsApp Cloud API
            url = f"{self.base_url}/{media_id}"
            # Facebook/WhatsApp Graph endpoints accept the access token as a
            # query parameter or Authorization header. Use query param to be
            # compatible with common setups and avoid preflight header issues.
            params = {"access_token": self.meta_api_key} if self.meta_api_key else None

            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            
            media_data = response.json()
            media_url = media_data.get("url")
            mime_type = media_data.get("mime_type", "application/octet-stream")
            
            if not media_url:
                logger.error("No media URL returned from WhatsApp API")
                return None
            
            logger.info(f"Media URL retrieved: {media_url[:50]}...")
            
            # Step 2: Download the actual media file
            # Step 2: Download the actual media file. The returned `media_url`
            # is often protected and requires the same access token. Use the
            # Authorization header when available, otherwise fall back to
            # passing the token as a query param.
            download_headers = {"Authorization": f"Bearer {self.meta_api_key}"} if self.meta_api_key else None
            download_params = None if download_headers else ({"access_token": self.meta_api_key} if self.meta_api_key else None)

            media_response = requests.get(media_url, headers=download_headers, params=download_params, timeout=30)
            media_response.raise_for_status()
            
            media_bytes = media_response.content
            
            # Generate filename based on mime type
            ext = self._get_extension_from_mime_type(mime_type)
            filename = f"media_{media_id}{ext}"
            
            logger.info(f"Media downloaded successfully: {len(media_bytes)} bytes, type: {mime_type}")
            return media_bytes, mime_type, filename
            
        except Exception as e:
            logger.error(f"Error downloading media: {e}")
            return None
    
    def process_image(
        self,
        media_id: str,
        media_url: Optional[str] = None
    ) -> Optional[Dict[str, str]]:
        """
        Process image from WhatsApp and prepare for LLM vision API
        
        Args:
            media_id: The media ID from WhatsApp message
            media_url: Optional direct URL to media (if already available)
            
        Returns:
            Dict with keys: 'base64' (base64-encoded image), 'mime_type', 'url' or None
        """
        try:
            logger.info(f"Processing image: media_id={media_id}")
            
            if media_url:
                # Use provided URL directly
                logger.info(f"Using provided media URL")
                media_bytes = self._download_from_url(media_url)
                if not media_bytes:
                    return None
                mime_type = self._detect_mime_type_from_bytes(media_bytes)
            else:
                # Download from WhatsApp API
                result = self.download_media_from_whatsapp(media_id)
                if not result:
                    return None
                media_bytes, mime_type, _ = result
            
            coerced = self._coerce_image_bytes(media_bytes)
            if coerced:
                media_bytes, mime_type = coerced
            if mime_type not in self.supported_images:
                logger.warning(f"Unsupported image type: {mime_type}")
                return None
            
            # Convert to base64
            base64_image = base64.b64encode(media_bytes).decode('utf-8')
            
            return {
                "base64": base64_image,
                "mime_type": mime_type,
                "url": media_url or f"{self.base_url}/{media_id}"
            }
            
        except Exception as e:
            logger.error(f"Error processing image: {e}")
            return None
    
    def process_audio(
        self,
        media_id: str,
        media_url: Optional[str] = None
    ) -> Optional[Dict[str, any]]:
        """
        Process audio from WhatsApp and prepare for transcription
        
        Args:
            media_id: The media ID from WhatsApp message
            media_url: Optional direct URL to media
            
        Returns:
            Dict with keys: 'bytes', 'mime_type', 'filename' or None
        """
        try:
            logger.info(f"Processing audio: media_id={media_id}")
            
            if media_url:
                # Use provided URL directly
                logger.info(f"Using provided media URL")
                media_bytes = self._download_from_url(media_url)
                if not media_bytes:
                    return None
                mime_type = self._detect_mime_type_from_bytes(media_bytes)
            else:
                # Download from WhatsApp API
                result = self.download_media_from_whatsapp(media_id)
                if not result:
                    return None
                media_bytes, mime_type, filename = result
            
            # Validate it's audio
            if mime_type not in self.supported_audio:
                logger.warning(f"Unsupported audio type: {mime_type}")
                return None
            
            # Generate appropriate filename for Whisper API
            ext = self._get_extension_from_mime_type(mime_type)
            filename = f"audio_{media_id}{ext}"
            
            return {
                "bytes": media_bytes,
                "mime_type": mime_type,
                "filename": filename,
                "size": len(media_bytes)
            }
            
        except Exception as e:
            logger.error(f"Error processing audio: {e}")
            return None
    
    def _download_from_url(self, url: str) -> Optional[bytes]:
        """Download file from URL. Instagram CDN links need a browser user agent."""
        try:
            headers = dict(_FETCH_HEADERS)
            params = None
            # If downloading from Graph endpoints, include token
            if self.meta_api_key and ("graph.instagram.com" in url or "graph.facebook.com" in url):
                headers["Authorization"] = f"Bearer {self.meta_api_key}"

            response = requests.get(url, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            return response.content

        except Exception as e:
            logger.error(f"Error downloading from URL: {e}")
            return None

    def _coerce_image_bytes(self, data: bytes) -> Optional[Tuple[bytes, str]]:
        """Return JPEG/PNG/GIF/WEBP bytes, converting phone formats Pillow can open."""
        if not data:
            return None
        mime_type = self._detect_mime_type_from_bytes(data)
        if mime_type in self.supported_images:
            return data, mime_type
        try:
            from PIL import Image

            image = Image.open(BytesIO(data))
            image = image.convert("RGB")
            output = BytesIO()
            image.save(output, format="JPEG", quality=85)
            jpeg = output.getvalue()
            if jpeg:
                return jpeg, "image/jpeg"
        except Exception as exc:
            logger.warning("Could not decode image bytes: %s", exc)
        return None
    
    def _detect_mime_type_from_bytes(self, data: bytes) -> str:
        """Detect MIME type from file bytes"""
        # Simple magic number detection
        if data.startswith(b'\xff\xd8\xff'):
            return "image/jpeg"
        elif data.startswith(b'\x89PNG'):
            return "image/png"
        elif data.startswith(b'GIF8'):
            return "image/gif"
        elif data.startswith(b'RIFF') and b'WEBP' in data[:12]:
            return "image/webp"
        elif data.startswith(b'\xff\xfb') or data.startswith(b'\xff\xfa'):
            return "audio/mpeg"
        elif data.startswith(b'ID3') or data.startswith(b'\xff\xfb'):
            return "audio/mpeg"
        else:
            return "application/octet-stream"
    
    def _get_extension_from_mime_type(self, mime_type: str) -> str:
        """Get file extension from MIME type"""
        extension_map = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "audio/mpeg": ".mp3",
            "audio/mp4": ".m4a",
            "audio/aac": ".aac",
            "audio/ogg": ".ogg"
        }
        return extension_map.get(mime_type, "")
    
    def validate_media(self, media_type: str, mime_type: str) -> bool:
        """Validate if media type is supported"""
        if media_type == "image":
            return mime_type in self.supported_images
        elif media_type == "audio":
            return mime_type in self.supported_audio
        return False
