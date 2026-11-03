# core/otp/service/otpservice.py

import hashlib
import hmac
import json
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Optional
import os
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from sqlalchemy.orm import Session
from core.otp.model.otp import OTP
from core.otp.dto.response.otp_send_response import OTPSendResponse
from core.wirepick.service.wirepickservice import WirepickSMSService, WirepickSMSException
from config import settings
import logging

logger = logging.getLogger(__name__)


class OTPService:
    def __init__(self, db: Session):
        self.db = db
        self.sms_service = WirepickSMSService()

    def generate_otp(self) -> str:
        """Generate a cryptographically strong 6-digit OTP"""
        return "".join(secrets.choice(string.digits) for _ in range(6))

    @staticmethod
    def _hash_otp(otp_code: str) -> str:
        return hashlib.sha256(otp_code.strip().encode("utf-8")).hexdigest()

    def _format_otp_message(self, otp_code: str) -> str:
        """Format OTP message for SMS"""
        seconds = int(settings.OTP_EXPIRE_SECONDS)
        if seconds >= 60 and seconds % 60 == 0:
            validity = f"{seconds // 60} minutes"
        else:
            validity = f"{seconds} seconds"
        return f"Your verification code is: {otp_code}. Valid for {validity}."

    def send_otp_phone(self, phone: str) -> OTPSendResponse:
        """Send OTP to phone number using Wirepick SMS"""
        try:
            otp_code = self.generate_otp()
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.OTP_EXPIRE_SECONDS)
            
            # Delete any existing OTP for this phone
            self.db.query(OTP).filter(OTP.phone == phone).delete()
            
            # Create new OTP record (store hash only)
            otp_record = OTP(
                phone=phone,
                otp=self._hash_otp(otp_code),
                expires_at=expires_at
            )
            
            self.db.add(otp_record)
            self.db.commit()
            self.db.refresh(otp_record)
            
            # Format the OTP message
            message = self._format_otp_message(otp_code)

            try:
                sms_result = self.sms_service.send_sms(phone, message)
                
                if sms_result.get('success'):
                    logger.info(f"OTP sent successfully to {phone}. Message ID: {sms_result.get('msgid')}")
                    
                    # You can store the msgid in your OTP record if needed
                    # otp_record.external_id = sms_result.get('msgid')
                    # self.db.commit()
                    
                    return OTPSendResponse(
                        success=True,
                        message="OTP sent successfully to your phone",
                        data={
                            "phone": phone,
                            "expires_at": expires_at.isoformat(),
                            "message_id": sms_result.get('msgid')  # Optional: return message ID for tracking
                        }
                    )
                else:
                    # SMS sending failed, rollback OTP creation
                    self.db.delete(otp_record)
                    self.db.commit()
                    
                    error_msg = sms_result.get('error', 'Unknown SMS error')
                    logger.error(f"Failed to send OTP via Wirepick: {error_msg}")
                    
                    return OTPSendResponse(
                        success=False,
                        message=f"Failed to send OTP. SMS provider error."
                    )
                    
            except WirepickSMSException as e:
                # SMS service error, rollback OTP creation
                self.db.delete(otp_record)
                self.db.commit()
                
                logger.error(f"Wirepick SMS error for {phone}: {str(e)}")
                return OTPSendResponse(
                    success=False,
                    message="Failed to send OTP. Please try again later."
                )
            
        except Exception as e:
            logger.error(f"Error sending OTP to phone {phone}: {str(e)}")
            return OTPSendResponse(
                success=False,
                message="Failed to send OTP. Please try again."
            )

    def send_otp_email(
        self,
        email: str,
        *,
        subject: Optional[str] = None,
        body_template: Optional[str] = None,
    ) -> OTPSendResponse:
        """Send OTP to email address.

        `body_template` may include `{otp}` and `{seconds}` placeholders.
        """
        try:
            otp_code = self.generate_otp()
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.OTP_EXPIRE_SECONDS)
            
            # Delete any existing OTP for this email
            self.db.query(OTP).filter(OTP.email == email).delete()
            
            # Create new OTP record (store hash only)
            otp_record = OTP(
                email=email,
                otp=self._hash_otp(otp_code),
                expires_at=expires_at
            )
            
            self.db.add(otp_record)
            self.db.commit()
            self.db.refresh(otp_record)

            seconds = int(settings.OTP_EXPIRE_SECONDS)
            subject = subject or "Your Autobus verification code"
            if body_template:
                body = body_template.replace("{otp}", otp_code).replace("{seconds}", str(seconds))
            else:
                body = f"Your verification code is: {otp_code}. Valid for {seconds} seconds."

            sender_domain = (
                getattr(settings, "ZEPTOMAIL_SENDER_DOMAIN", None)
                or os.getenv("ZEPTOMAIL_SENDER_DOMAIN", "useautobus.com")
            ).strip()
            from_email = settings.ZEPTOMAIL_FROM_EMAIL or f"no-reply@{sender_domain}"
            token = self._zeptomail_token()

            if not token:
                self.db.delete(otp_record)
                self.db.commit()
                logger.error("ZEPTOMAIL_SMTP_PASSWORD/ZEPTOMAIL_API_TOKEN not set; cannot send OTP email")
                return OTPSendResponse(success=False, message="Email service not configured")

            sent, send_error = self._deliver_email(from_email, email, subject, body, token)
            if sent:
                logger.info("OTP email sent successfully to %s", email)
                return OTPSendResponse(
                    success=True,
                    message="OTP sent successfully to your email",
                    data={"email": email, "expires_at": expires_at.isoformat()},
                )

            self.db.delete(otp_record)
            self.db.commit()
            logger.error("Failed to send OTP email to %s: %s", email, send_error)
            return OTPSendResponse(
                success=False,
                message=send_error or "Failed to send OTP email. Please try again.",
            )
            
        except Exception as e:
            logger.error(f"Error sending OTP to email {email}: {str(e)}")
            return OTPSendResponse(
                success=False,
                message="Failed to send OTP. Please try again."
            )

    def send_email(self, email: str, *, subject: str, body: str) -> OTPSendResponse:
        """Send a plain email without creating an OTP."""
        try:
            sender_domain = (
                getattr(settings, "ZEPTOMAIL_SENDER_DOMAIN", None)
                or os.getenv("ZEPTOMAIL_SENDER_DOMAIN", "useautobus.com")
            ).strip()
            from_email = settings.ZEPTOMAIL_FROM_EMAIL or f"no-reply@{sender_domain}"
            token = self._zeptomail_token()
            if not token:
                logger.error("ZEPTOMAIL_SMTP_PASSWORD/ZEPTOMAIL_API_TOKEN not set; cannot send email")
                return OTPSendResponse(success=False, message="Email service not configured")

            sent, send_error = self._deliver_email(from_email, email, subject, body, token)
            if sent:
                logger.info("Email sent successfully to %s", email)
                return OTPSendResponse(success=True, message="Email sent", data={"email": email})

            logger.error("Failed to send email to %s: %s", email, send_error)
            return OTPSendResponse(
                success=False,
                message=send_error or "Failed to send email. Please try again.",
            )
        except Exception as e:
            logger.error(f"Error sending email to {email}: {str(e)}")
            return OTPSendResponse(success=False, message="Failed to send email. Please try again.")

    @staticmethod
    def _zeptomail_token() -> str:
        raw = (
            settings.ZEPTOMAIL_SMTP_PASSWORD
            or os.getenv("ZEPTOMAIL_API_TOKEN")
            or ""
        ).strip()
        prefix = "zoho-enczapikey "
        if raw.lower().startswith(prefix):
            return raw[len(prefix) :].strip()
        return raw

    def _deliver_email(
        self,
        from_email: str,
        to_email: str,
        subject: str,
        body: str,
        token: str,
    ) -> tuple[bool, str]:
        api_error = self._send_zeptomail_http(from_email, to_email, subject, body, token)
        if api_error is None:
            return True, ""
        logger.warning("ZeptoMail HTTP send failed, trying SMTP: %s", api_error)
        smtp_error = self._send_zeptomail_smtp(from_email, to_email, subject, body, token)
        if smtp_error is None:
            return True, ""
        return False, api_error or smtp_error

    def _send_zeptomail_http(
        self,
        from_email: str,
        to_email: str,
        subject: str,
        body: str,
        token: str,
    ) -> Optional[str]:
        api_url = (
            os.getenv("ZEPTOMAIL_API_URL") or "https://api.zeptomail.com/v1.1/email"
        ).strip()
        payload = {
            "from": {"address": from_email, "name": "Autobus Admin"},
            "to": [{"email_address": {"address": to_email}}],
            "subject": subject,
            "textbody": body,
        }
        request = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Zoho-enczapikey {token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if 200 <= response.status < 300:
                    return None
                return f"ZeptoMail API returned HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                raw = exc.read().decode("utf-8", errors="replace")
                parsed = json.loads(raw)
                err = parsed.get("error") or parsed
                if isinstance(err, dict):
                    detail = str(err.get("message") or err.get("details") or raw[:240])
                else:
                    detail = raw[:240]
            except Exception:
                detail = str(exc)
            return f"ZeptoMail rejected the email ({exc.code}): {detail}".strip()
        except Exception as exc:
            return f"Could not reach ZeptoMail API: {exc}"

    def _send_zeptomail_smtp(
        self,
        from_email: str,
        to_email: str,
        subject: str,
        body: str,
        token: str,
    ) -> Optional[str]:
        smtp_host = settings.ZEPTOMAIL_SMTP_HOST
        smtp_port = settings.ZEPTOMAIL_SMTP_PORT
        smtp_username = settings.ZEPTOMAIL_SMTP_USERNAME or "emailapikey"
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_email
        msg["To"] = to_email
        msg.set_content(body)
        context = ssl.create_default_context()
        try:
            if smtp_port == 465:
                with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=30) as server:
                    server.login(smtp_username, token)
                    server.send_message(msg)
            else:
                with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
                    server.ehlo()
                    server.starttls(context=context)
                    server.ehlo()
                    server.login(smtp_username, token)
                    server.send_message(msg)
            return None
        except smtplib.SMTPAuthenticationError:
            return "Email server rejected login. Check the ZeptoMail send-mail token."
        except Exception as exc:
            return f"SMTP send failed: {exc}"

    def validate_otp(
        self,
        phone: Optional[str] = None,
        email: Optional[str] = None,
        otp: str = None,
        consume: bool = True,
    ) -> bool:
        """Validate OTP for phone or email. Set consume=False to peek without deleting."""
        try:
            if not otp:
                return False

            query = self.db.query(OTP)
            if phone:
                query = query.filter(OTP.phone == phone)
            elif email:
                query = query.filter(OTP.email == email)
            else:
                return False

            otp_record = query.first()
            if not otp_record:
                return False

            if otp_record.is_expired():
                self.db.delete(otp_record)
                self.db.commit()
                return False

            candidate = otp.strip()
            stored = otp_record.otp or ""
            # Prefer hashed compare; allow legacy plaintext rows during migration
            hashed_ok = hmac.compare_digest(stored, self._hash_otp(candidate))
            legacy_ok = len(stored) <= 6 and hmac.compare_digest(stored, candidate)
            if not (hashed_ok or legacy_ok):
                return False

            if consume:
                self.db.delete(otp_record)
                self.db.commit()
            return True

        except Exception as e:
            logger.error(f"Error validating OTP: {str(e)}")
            return False

    def cleanup_expired_otps(self):
        """Clean up expired OTP records"""
        try:
            current_time = datetime.now(timezone.utc)
            expired_count = self.db.query(OTP).filter(OTP.expires_at < current_time).delete()
            self.db.commit()
            logger.info(f"Cleaned up {expired_count} expired OTP records")
        except Exception as e:
            logger.error(f"Error cleaning up expired OTPs: {str(e)}")