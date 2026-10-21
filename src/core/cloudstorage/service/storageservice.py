import os
import io
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from enum import Enum
from typing import Iterator, Optional, Tuple, Union
from urllib.parse import unquote, urlparse
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_shared_storage: Optional["StorageService"] = None


def object_key_from_url(url: str, bucket: Optional[str] = None) -> Optional[str]:
    """Extract an S3 object key from a stored Contabo/S3 URL or raw key.

    Product images persist the upload-time presigned URL. The path still identifies
    the object after the query signature expires or is truncated.
    """
    raw = (url or "").strip()
    if not raw:
        return None

    if not raw.startswith(("http://", "https://")):
        if raw.startswith("operations/"):
            return raw.split("?", 1)[0]
        return None

    parsed = urlparse(raw)
    path = unquote(parsed.path or "").lstrip("/")
    if not path:
        return None

    bucket_name = (bucket or "").strip()
    first, sep, rest = path.partition("/")
    if not sep:
        return path if path.startswith("operations/") else None

    if bucket_name and first == bucket_name:
        return rest or None
    if bucket_name and first.endswith(f":{bucket_name}"):
        return rest or None
    if ":" in first and rest.startswith("operations/"):
        return rest
    if path.startswith("operations/"):
        return path
    return None


def get_storage_service() -> "StorageService":
    global _shared_storage
    if _shared_storage is None:
        _shared_storage = StorageService()
    return _shared_storage


def refresh_public_object_url(stored_url: str) -> str:
    """Return a fresh presigned GET URL when the stored value is a storage object."""
    url = (stored_url or "").strip()
    if not url:
        return stored_url or ""
    try:
        return get_storage_service().refresh_object_url(url)
    except Exception as e:
        logger.warning("Could not refresh object URL: %s", e)
        return url


class StorageFolder(str, Enum):
    profile_images = "profile-images"
    product_images = "product-images"
    chatbot_files = "chatbot-files"
    records_files = "records-files"
    generated_images = "generated-images"
    generated_videos = "generated-videos"
    digital_marketing = "digital-marketing"
    automedia = "automedia"
    instagram_publish = "instagram-publish"


class StorageService:
    """Contabo S3-compatible storage implementation used by the app.

    It reads configuration from environment variables:
      - CONTABO_ACCESS_KEY
      - CONTABO_SECRET_KEY
      - CONTABO_ENDPOINT
      - CONTABO_BUCKET

    Methods:
      - upload_file(file_obj, file_name, content_type=None) -> str (blob url)
      - download_file(file_name, destination_path) -> str
    """

    def __init__(self):
        self.access_key = os.getenv("CONTABO_ACCESS_KEY")
        self.secret_key = os.getenv("CONTABO_SECRET_KEY")
        self.endpoint = os.getenv("CONTABO_ENDPOINT", "https://usc1.contabostorage.com")
        self.bucket = os.getenv("CONTABO_BUCKET")

        if not self.access_key or not self.secret_key or not self.bucket:
            raise ValueError(
                "Contabo storage credentials and bucket must be set: "
                "CONTABO_ACCESS_KEY, CONTABO_SECRET_KEY, and CONTABO_BUCKET"
            )

        # Initialize S3 client with Contabo endpoint
        self.s3_client = boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name="us-east-1"  # Required but not used by Contabo
        )

        # Ensure bucket exists (create if not). If it already exists, ignore the error.
        try:
            self.s3_client.head_bucket(Bucket=self.bucket)
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                try:
                    self.s3_client.create_bucket(Bucket=self.bucket)
                except ClientError:
                    pass  # Bucket creation not permitted; continue.
            else:
                pass  # Other errors; continue.

    def refresh_object_url(self, stored_url: str, expires_in: int = 86400 * 7) -> str:
        """Mint a new presigned GET URL from a stored upload URL or object key."""
        url = (stored_url or "").strip()
        if not url:
            return stored_url or ""
        key = object_key_from_url(url, bucket=self.bucket)
        if not key:
            return url
        return self.s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_in,
        )

    @staticmethod
    def resolve_subfolder(
        folder: Optional[Union["StorageFolder", str]] = None,
        subfolder: str = "operations/",
    ) -> str:
        """Resolve a logical folder to an S3 prefix (subfolder)."""
        if folder is None:
            resolved = subfolder
        else:
            try:
                folder_enum = folder if isinstance(folder, StorageFolder) else StorageFolder(str(folder))
            except Exception as e:
                raise ValueError(
                    f"Invalid storage folder '{folder}'. Allowed: {[f.value for f in StorageFolder]}"
                ) from e

            # Keep legacy `operations/` prefix for consistency across the app.
            resolved = f"operations/{folder_enum.value}/"

        if not resolved.endswith("/"):
            resolved += "/"
        return resolved

    def upload_file(
        self,
        file_obj,
        file_name: str,
        content_type: str | None = None,
        timeout_seconds: int = 30,
        subfolder: str = "operations/",
        folder: Optional[Union["StorageFolder", str]] = None,
        metadata: Optional[dict[str, str]] = None,
    ) -> str:
        """Upload a file-like object to Contabo S3 storage and return the object URL.

        file_obj must be a readable file-like object (e.g. UploadFile.file from FastAPI).

        Args:
            file_obj: File-like object to upload
            file_name: Name/path for the object
            content_type: MIME type for the object
            timeout_seconds: Maximum seconds to wait for upload (default: 30)
            subfolder: S3 subfolder path (default: "operations/")
            folder: Logical folder name (preferred). If provided, overrides subfolder.

        Returns:
            str: URL of the uploaded object

        Raises:
            TimeoutError: If upload takes longer than timeout_seconds
            Exception: Other S3 storage exceptions
        """
        subfolder = self.resolve_subfolder(folder=folder, subfolder=subfolder)
        
        # Construct full S3 key with subfolder
        s3_key = f"{subfolder}{file_name}"
        
        # Make sure we start reading from beginning
        try:
            file_obj.seek(0)
        except Exception:
            pass

        # Define upload function to run in thread
        def _upload():
            try:
                extra_args = {}
                if content_type:
                    extra_args["ContentType"] = content_type
                if metadata:
                    extra_args["Metadata"] = metadata

                self.s3_client.upload_fileobj(
                    file_obj,
                    self.bucket,
                    s3_key,
                    ExtraArgs=extra_args if extra_args else None
                )
                
                # Generate public URL for the uploaded object using pre-signed URL
                # This ensures the URL is in the correct Contabo format and publicly accessible
                url = self.s3_client.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': self.bucket, 'Key': s3_key},
                    ExpiresIn=31536000  # 1 year expiration
                )
                return url
            except Exception as e:
                logger.error(f"Contabo S3 upload error: {str(e)}")
                raise

        # Execute upload with hard timeout using ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_upload)
            try:
                # Wait for result with timeout
                result = future.result(timeout=timeout_seconds)
                logger.info(f"Successfully uploaded {file_name} to {subfolder} in Contabo S3 storage")
                return result
            except FuturesTimeoutError:
                logger.error(f"Upload timeout after {timeout_seconds}s for {file_name}")
                # Cancel the future (though the upload might still continue in background)
                future.cancel()
                raise TimeoutError(f"Contabo S3 upload timed out after {timeout_seconds} seconds")
            except Exception as e:
                logger.error(f"Upload failed for {file_name}: {str(e)}")
                raise

    def download_file(
        self,
        file_name: str,
        destination_path: str,
        subfolder: str = "operations/",
        folder: Optional[Union["StorageFolder", str]] = None,
    ) -> str:
        """Download file from Contabo S3 to local file path.

        Args:
            file_name: Name/path of the object in S3
            destination_path: Local path to save the file
            subfolder: S3 subfolder path (default: "operations/")
            folder: Logical folder name (preferred). If provided, overrides subfolder.

        Returns:
            str: Confirmation message

        Raises:
            Exception: S3 storage exceptions if file not found
        """
        subfolder = self.resolve_subfolder(folder=folder, subfolder=subfolder)
        
        # Construct full S3 key with subfolder
        s3_key = f"{subfolder}{file_name}"
        
        try:
            self.s3_client.download_file(
                self.bucket,
                s3_key,
                destination_path
            )
            logger.info(f"Successfully downloaded {file_name} from {subfolder} in Contabo S3 to {destination_path}")
            return f"Downloaded {file_name} to {destination_path}"
        except ClientError as e:
            logger.error(f"Contabo S3 download error for {file_name}: {str(e)}")
            raise

    def download_file_bytes(
        self,
        file_name: str,
        folder: Optional[Union["StorageFolder", str]] = None,
        subfolder: str = "operations/",
    ) -> bytes:
        """Download an object from Contabo S3 into memory."""
        subfolder = self.resolve_subfolder(folder=folder, subfolder=subfolder)
        s3_key = f"{subfolder}{file_name}"
        try:
            buf = io.BytesIO()
            self.s3_client.download_fileobj(self.bucket, s3_key, buf)
            return buf.getvalue()
        except ClientError as e:
            logger.error(f"Contabo S3 download error for {file_name}: {str(e)}")
            raise

    def list_files(
        self,
        folder: Optional[Union["StorageFolder", str]] = None,
        subfolder: str = "operations/",
        prefix: str = "",
        max_keys: int = 1000,
    ) -> list[dict]:
        """List objects under a given folder + prefix.

        Returns a list of dicts:
        { "key": str, "url": str, "last_modified": datetime | None, "metadata": dict }.
        """
        subfolder = self.resolve_subfolder(folder=folder, subfolder=subfolder)
        full_prefix = f"{subfolder}{prefix}"

        results: list[dict] = []
        continuation_token: str | None = None

        while True:
            params = {
                "Bucket": self.bucket,
                "Prefix": full_prefix,
                "MaxKeys": max_keys,
            }
            if continuation_token:
                params["ContinuationToken"] = continuation_token

            resp = self.s3_client.list_objects_v2(**params)
            for obj in resp.get("Contents", []) or []:
                key = obj.get("Key")
                if not key or key.endswith("/"):
                    continue

                url = self.s3_client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self.bucket, "Key": key},
                    ExpiresIn=31536000,
                )
                object_metadata: dict[str, str] = {}
                try:
                    head = self.s3_client.head_object(Bucket=self.bucket, Key=key)
                    raw_meta = head.get("Metadata") or {}
                    if isinstance(raw_meta, dict):
                        object_metadata = {
                            str(k): str(v) for k, v in raw_meta.items() if v is not None
                        }
                except ClientError as e:
                    logger.warning("Could not read metadata for %s: %s", key, e)

                results.append(
                    {
                        "key": key,
                        "url": url,
                        "last_modified": obj.get("LastModified"),
                        "metadata": object_metadata,
                    }
                )

            if resp.get("IsTruncated"):
                continuation_token = resp.get("NextContinuationToken")
                if not continuation_token:
                    break
            else:
                break

        return results

    def head_object_meta(
        self,
        file_name: str,
        folder: Optional[Union["StorageFolder", str]] = None,
        subfolder: str = "operations/",
    ) -> Tuple[str, int]:
        """Return ``(content_type, content_length)`` without downloading the object."""
        subfolder = self.resolve_subfolder(folder=folder, subfolder=subfolder)
        s3_key = f"{subfolder}{file_name}"
        try:
            head = self.s3_client.head_object(Bucket=self.bucket, Key=s3_key)
        except ClientError as e:
            logger.error(f"Contabo S3 head_object error for {file_name}: {str(e)}")
            raise
        content_type = str(head.get("ContentType") or "application/octet-stream")
        length = head.get("ContentLength")
        if length is None:
            raise ClientError(
                {"Error": {"Code": "NotFound", "Message": "Missing ContentLength"}},
                "HeadObject",
            )
        return content_type, int(length)

    def iter_object_chunks(
        self,
        file_name: str,
        folder: Optional[Union["StorageFolder", str]] = None,
        subfolder: str = "operations/",
        chunk_size: int = 65536,
        start: Optional[int] = None,
        end: Optional[int] = None,
    ) -> Tuple[str, Optional[int], Iterator[bytes]]:
        """Stream an object from Contabo S3 without loading it all into memory."""
        subfolder = self.resolve_subfolder(folder=folder, subfolder=subfolder)
        s3_key = f"{subfolder}{file_name}"
        params: dict = {"Bucket": self.bucket, "Key": s3_key}
        if start is not None:
            last = "" if end is None else str(end)
            params["Range"] = f"bytes={start}-{last}"
        try:
            obj = self.s3_client.get_object(**params)
        except ClientError as e:
            logger.error(f"Contabo S3 get_object error for {file_name}: {str(e)}")
            raise
        body = obj["Body"]
        content_type = str(obj.get("ContentType") or "application/octet-stream")
        length = obj.get("ContentLength")
        content_length = int(length) if length is not None else None

        def _chunks() -> Iterator[bytes]:
            try:
                while True:
                    data = body.read(chunk_size)
                    if not data:
                        break
                    yield data
            finally:
                try:
                    body.close()
                except Exception:
                    pass

        return content_type, content_length, _chunks()

    def delete_file(
        self,
        file_name: str,
        folder: Optional[Union["StorageFolder", str]] = None,
        subfolder: str = "operations/",
    ) -> None:
        """Delete an object from Contabo S3."""
        subfolder = self.resolve_subfolder(folder=folder, subfolder=subfolder)
        s3_key = f"{subfolder}{file_name}"
        try:
            self.s3_client.delete_object(Bucket=self.bucket, Key=s3_key)
            logger.info(f"Successfully deleted {s3_key} from Contabo S3")
        except ClientError as e:
            logger.error(f"Contabo S3 delete error for {s3_key}: {str(e)}")
            raise
