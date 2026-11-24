"""Prepare marketing media so Instagram / Postiz can fetch and publish it.

Instagram Graph publishing fails on:
- files over 8 MiB, non-JPEG, or aspect ratios outside 4:5–1.91:1 (feed)
- URLs Meta cannot fetch (signed query strings, private buckets)
- Postiz concatenating `image_url=${path}` so `&` in Contabo presigned URLs
  splits the Graph query and Instagram never sees the real file
- videos whose path has no `.mp4` (Postiz then sends them as images)

This module converts images to JPEG, fits the ratio, re-hosts under a clean
public path (`/api/v1/social/media/public/{uuid}.jpg|mp4`) with no query string.
"""

from __future__ import annotations

import copy
import io
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple
from urllib.parse import urlparse

import requests
from PIL import Image, ImageOps, UnidentifiedImageError

logger = logging.getLogger(__name__)

CropMode = Literal["feed", "story", "none"]

FEED_MIN_RATIO = 4.0 / 5.0  # 0.8 portrait
FEED_MAX_RATIO = 1.91
STORY_RATIO = 9.0 / 16.0
MAX_FEED_WIDTH = 1080
MAX_STORY_WIDTH = 1080
MAX_STORY_HEIGHT = 1920
MIN_WIDTH = 320
# Instagram hard-limit is 8 MiB; stay under so JPEG headers / re-encode don't trip it.
MAX_IMAGE_BYTES = 7 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".heif"}
INSTAGRAM_TYPES = {"instagram", "instagram-standalone"}
PUBLIC_MEDIA_PATH = "/api/v1/social/media/public/"
# TikTok Direct Post rejects anything outside this window ("video frame rate is invalid").
TIKTOK_MIN_FPS = 23.0
TIKTOK_MAX_FPS = 60.0
TIKTOK_TARGET_FPS = 30
_FPS_PROBE_TIMEOUT = 30
_FPS_TRANSCODE_TIMEOUT = 300


class InstagramMediaPrepareError(RuntimeError):
    """Raised when a source file cannot be made Instagram-safe."""


def public_api_base() -> str:
    return (
        os.getenv("AUTOBUS_PUBLIC_API_URL", "").strip()
        or os.getenv("PUBLIC_API_URL", "").strip()
        or "https://api.useautobus.com"
    ).rstrip("/")


def public_media_url(file_name: str) -> str:
    return f"{public_api_base()}{PUBLIC_MEDIA_PATH}{file_name}"


def is_prepared_public_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.query:
        return False
    return PUBLIC_MEDIA_PATH in (parsed.path or "")


def _path_ext(url: str) -> str:
    path = urlparse(url).path.lower()
    for ext in VIDEO_EXTENSIONS | IMAGE_EXTENSIONS:
        if path.endswith(ext):
            return ext
    name = path.rsplit("/", 1)[-1]
    if "." in name:
        return "." + name.rsplit(".", 1)[-1].lower()
    return ""


def is_video_url(url: str) -> bool:
    return _path_ext(url) in VIDEO_EXTENSIONS


def parse_http_byte_range(header: str, total: int) -> Optional[Tuple[int, int]]:
    """Parse ``Range: bytes=start-end`` into inclusive indexes.

    Postiz TikTok FILE_UPLOAD requires HTTP 206 for these requests. Returns
    None when the header is missing or not a single byte range.
    """
    raw = (header or "").strip()
    if not raw or total <= 0:
        return None
    lowered = raw.lower()
    if not lowered.startswith("bytes="):
        return None
    spec = raw.split("=", 1)[1].strip()
    if "," in spec:
        return None
    start_s, sep, end_s = spec.partition("-")
    if not sep:
        return None
    start_s = start_s.strip()
    end_s = end_s.strip()
    try:
        if start_s == "":
            suffix = int(end_s)
            if suffix <= 0:
                return None
            start = max(total - suffix, 0)
            end = total - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else total - 1
    except ValueError:
        return None
    if start < 0 or end < start or start >= total:
        return None
    return start, min(end, total - 1)


def select_instagram_media_urls(urls: Iterable[str], *, is_story: bool) -> List[str]:
    """Pick Instagram-legal attachments: one story item, one reel, or 1–10 images."""
    cleaned = [u.strip() for u in urls if u and str(u).strip()]
    videos = [u for u in cleaned if is_video_url(u)]
    images = [u for u in cleaned if not is_video_url(u)]
    if is_story:
        if videos:
            return videos[:1]
        return images[:1]
    if videos and images:
        logger.warning(
            "[IG media] Mixed image+video on one Instagram post; using images only"
        )
        return images[:10]
    if videos:
        return videos[:1]
    return images[:10]


def _center_crop_to_ratio(im: Image.Image, target_ratio: float) -> Image.Image:
    width, height = im.size
    if width <= 0 or height <= 0:
        return im
    current = width / height
    if abs(current - target_ratio) < 0.002:
        return im
    if current > target_ratio:
        new_w = max(1, int(round(height * target_ratio)))
        left = max(0, (width - new_w) // 2)
        return im.crop((left, 0, left + new_w, height))
    new_h = max(1, int(round(width / target_ratio)))
    top = max(0, (height - new_h) // 2)
    return im.crop((0, top, width, top + new_h))


def fit_image_for_instagram(im: Image.Image, *, crop: CropMode) -> Image.Image:
    """Center-crop / scale so the image meets Instagram feed or story rules."""
    if crop == "story":
        im = _center_crop_to_ratio(im, STORY_RATIO)
        width, height = im.size
        scale = min(MAX_STORY_WIDTH / max(width, 1), MAX_STORY_HEIGHT / max(height, 1), 1.0)
        if width < MIN_WIDTH:
            scale = max(scale, MIN_WIDTH / max(width, 1))
        if abs(scale - 1.0) > 1e-6:
            im = im.resize(
                (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
                Image.Resampling.LANCZOS,
            )
        return im

    if crop == "feed":
        width, height = im.size
        ratio = width / max(height, 1)
        if ratio < FEED_MIN_RATIO:
            im = _center_crop_to_ratio(im, FEED_MIN_RATIO)
        elif ratio > FEED_MAX_RATIO:
            im = _center_crop_to_ratio(im, FEED_MAX_RATIO)

    width, height = im.size
    scale = 1.0
    if width > MAX_FEED_WIDTH:
        scale = min(scale, MAX_FEED_WIDTH / width)
    # 4:5 at 1080 wide is 1350 tall; keep a modest cap for generic images too.
    max_h = 1350 if crop == "feed" else 1920
    if height > max_h:
        scale = min(scale, max_h / height)
    if width < MIN_WIDTH:
        scale = MIN_WIDTH / max(width, 1)
    if abs(scale - 1.0) > 1e-6:
        im = im.resize(
            (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
            Image.Resampling.LANCZOS,
        )
    return im


def _flatten_to_rgb(im: Image.Image) -> Image.Image:
    if getattr(im, "n_frames", 1) > 1:
        im.seek(0)
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    if im.mode != "RGB":
        return im.convert("RGB")
    return im


def encode_jpeg(im: Image.Image, *, max_bytes: int = MAX_IMAGE_BYTES) -> bytes:
    """JPEG-encode, dropping quality then scale, until under max_bytes."""
    working = im
    for _ in range(6):
        for quality in (85, 80, 75, 70, 65, 60, 50, 40):
            buf = io.BytesIO()
            working.save(
                buf,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
                subsampling=2,
            )
            data = buf.getvalue()
            if len(data) <= max_bytes:
                return data
        width, height = working.size
        if width < MIN_WIDTH or height < 2:
            break
        working = working.resize(
            (max(MIN_WIDTH, int(width * 0.8)), max(2, int(height * 0.8))),
            Image.Resampling.LANCZOS,
        )
    raise InstagramMediaPrepareError(
        "Could not compress the image under Instagram's 8 MB limit"
    )


def image_bytes_to_instagram_jpeg(raw: bytes, *, crop: CropMode) -> bytes:
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except UnidentifiedImageError as exc:
        raise InstagramMediaPrepareError(
            "Instagram only accepts JPEG or PNG images (not HEIC/WebP from some phones)"
        ) from exc
    except Exception as exc:
        raise InstagramMediaPrepareError(f"Could not read image: {exc}") from exc

    im = ImageOps.exif_transpose(im)
    im = _flatten_to_rgb(im)
    im = fit_image_for_instagram(im, crop=crop)
    return encode_jpeg(im)


def _looks_like_mp4(data: bytes) -> bool:
    return len(data) >= 12 and data[4:8] == b"ftyp"


def _looks_like_image(data: bytes) -> bool:
    if data[:3] == b"\xff\xd8\xff":
        return True
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


def _stamp_product_code(jpeg: bytes, source_url: str) -> bytes:
    """Print the catalogue code on images Autobus publishes. Failures stay unstamped."""
    try:
        from core.product.service.image_match_service import stamp_outbound_jpeg

        return stamp_outbound_jpeg(jpeg, source_url)
    except Exception as exc:
        logger.warning("[IG media] Product code stamp skipped: %s", exc)
        return jpeg


def _download(url: str) -> Tuple[bytes, str]:
    headers = {
        "User-Agent": "AutobusInstagramMediaPrepare/1.0",
        "Accept": "*/*",
    }
    try:
        response = requests.get(url, headers=headers, timeout=60, stream=True)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise InstagramMediaPrepareError(
            f"Could not download media for Instagram: {exc}"
        ) from exc

    content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
    chunks: List[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_DOWNLOAD_BYTES:
            raise InstagramMediaPrepareError(
                "Media file is too large to prepare for Instagram"
            )
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise InstagramMediaPrepareError("Downloaded media was empty")
    return data, content_type


def parse_frame_rate(value: Optional[str]) -> Optional[float]:
    """Parse an ffprobe rate such as ``30000/1001`` or ``30``."""
    raw = (value or "").strip()
    if not raw or raw.upper() in {"N/A", "0/0"}:
        return None
    if "/" in raw:
        num_s, den_s = raw.split("/", 1)
        try:
            den = float(den_s)
            if den == 0:
                return None
            rate = float(num_s) / den
        except ValueError:
            return None
    else:
        try:
            rate = float(raw)
        except ValueError:
            return None
    return rate if rate > 0 else None


def tiktok_frame_rate_needs_correction(
    avg_fps: Optional[float],
    stream_fps: Optional[float],
) -> bool:
    """True when TikTok would reject the clip, or the rate is variable.

    A constant 24, 30, or 60 fps file is left alone. Rates under 23 or over 60,
    and clips whose average and stream rates disagree, are re-encoded.
    """
    known = [rate for rate in (avg_fps, stream_fps) if rate is not None and rate > 0]
    if not known:
        return False
    if any(rate < TIKTOK_MIN_FPS or rate > TIKTOK_MAX_FPS for rate in known):
        return True
    if (
        avg_fps is not None
        and stream_fps is not None
        and abs(avg_fps - stream_fps) > 1.0
    ):
        return True
    return False


def _probe_frame_rates(ffprobe: str, path: str) -> Tuple[Optional[float], Optional[float]]:
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate,r_frame_rate",
            "-of",
            "default=noprint_wrappers=1",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=_FPS_PROBE_TIMEOUT,
        check=False,
    )
    avg: Optional[float] = None
    stream: Optional[float] = None
    for line in (result.stdout or "").splitlines():
        key, _, value = line.partition("=")
        if key == "avg_frame_rate":
            avg = parse_frame_rate(value)
        elif key == "r_frame_rate":
            stream = parse_frame_rate(value)
    return avg, stream


def _transcode_constant_fps(ffmpeg: str, source: str, dest: str, fps: int) -> None:
    command = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        source,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        f"fps={fps},format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        dest,
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=_FPS_TRANSCODE_TIMEOUT,
        check=False,
    )
    if result.returncode != 0 or not os.path.isfile(dest) or os.path.getsize(dest) < 32:
        detail = (result.stderr or result.stdout or "ffmpeg failed").strip()[:400]
        logger.warning("[TikTok media] frame-rate convert failed: %s", detail)
        raise InstagramMediaPrepareError(
            "This video's frame rate isn't one TikTok accepts (23–60 fps), "
            "and we couldn't convert it. Try another video."
        )


def ensure_tiktok_frame_rate(data: bytes) -> bytes:
    """Re-encode to a constant 30 fps when the source would fail TikTok's check."""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        logger.warning("[TikTok media] ffmpeg/ffprobe missing; posting video without a frame-rate check")
        return data
    with tempfile.TemporaryDirectory(prefix="tiktok-fps-") as tmp:
        source = os.path.join(tmp, "in.mp4")
        dest = os.path.join(tmp, "out.mp4")
        with open(source, "wb") as handle:
            handle.write(data)
        try:
            avg_fps, stream_fps = _probe_frame_rates(ffprobe, source)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("[TikTok media] frame-rate probe failed: %s", exc)
            return data
        if not tiktok_frame_rate_needs_correction(avg_fps, stream_fps):
            logger.info(
                "[TikTok media] frame rate ok avg=%s stream=%s",
                avg_fps,
                stream_fps,
            )
            return data
        logger.info(
            "[TikTok media] frame rate invalid avg=%s stream=%s; re-encoding at %s fps",
            avg_fps,
            stream_fps,
            TIKTOK_TARGET_FPS,
        )
        try:
            _transcode_constant_fps(ffmpeg, source, dest, TIKTOK_TARGET_FPS)
        except subprocess.TimeoutExpired as exc:
            raise InstagramMediaPrepareError(
                "This video's frame rate isn't one TikTok accepts (23–60 fps), "
                "and converting it took too long. Try a shorter video."
            ) from exc
        with open(dest, "rb") as handle:
            return handle.read()


def payload_has_attached_media(payload: Dict[str, Any]) -> bool:
    """True when a Postiz payload includes any media path, even a local blob URL."""
    posts = payload.get("posts")
    if not isinstance(posts, list):
        return False
    for post in posts:
        if not isinstance(post, dict):
            continue
        value = post.get("value")
        if not isinstance(value, list):
            continue
        for block in value:
            if not isinstance(block, dict):
                continue
            images = block.get("image")
            if isinstance(images, dict):
                images = [images]
            if not isinstance(images, list):
                continue
            if any(isinstance(item, dict) and str(item.get("path") or "").strip() for item in images):
                return True
    return False


class InstagramMediaPrepareService:
    """Download, normalize, and re-host media for Instagram / Postiz publishing."""

    def __init__(self, storage: Optional[Any] = None) -> None:
        if storage is not None:
            self._storage = storage
        else:
            from core.cloudstorage.service.storageservice import StorageService

            self._storage = StorageService()
        self._cache: Dict[Tuple[str, CropMode, bool], str] = {}

    def _upload(self, data: bytes, file_name: str, content_type: str) -> str:
        self._storage.upload_file(
            io.BytesIO(data),
            file_name,
            content_type=content_type,
            folder="instagram-publish",
            timeout_seconds=120,
        )
        return public_media_url(file_name)

    def prepare_url(self, url: str, *, crop: CropMode = "feed", require_video: bool = False) -> str:
        source = (url or "").strip()
        if not source:
            raise InstagramMediaPrepareError("Media URL is empty")
        if is_prepared_public_url(source):
            return source

        cache_key = (source, crop, require_video)
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        data, content_type = _download(source)
        ctype = content_type.lower()
        treat_as_video = (
            ctype.startswith("video/")
            or is_video_url(source)
            or _looks_like_mp4(data)
        ) and not _looks_like_image(data)

        if require_video and not treat_as_video:
            raise InstagramMediaPrepareError(
                "TikTok only accepts a video. Please try a video instead of a photo."
            )

        if not treat_as_video:
            try:
                jpeg = image_bytes_to_instagram_jpeg(data, crop=crop)
            except InstagramMediaPrepareError:
                if ctype.startswith("video/") or _looks_like_mp4(data) or is_video_url(source):
                    treat_as_video = True
                else:
                    raise
            else:
                jpeg = _stamp_product_code(jpeg, source)
                file_name = f"{uuid.uuid4()}.jpg"
                public = self._upload(jpeg, file_name, "image/jpeg")
                self._cache[cache_key] = public
                logger.info("[IG media] Prepared image %s -> %s (%s bytes)", source[:80], public, len(jpeg))
                return public

        if require_video:
            data = ensure_tiktok_frame_rate(data)
        file_name = f"{uuid.uuid4()}.mp4"
        public = self._upload(data, file_name, ctype if ctype.startswith("video/") else "video/mp4")
        self._cache[cache_key] = public
        logger.info("[IG media] Re-hosted video %s -> %s (%s bytes)", source[:80], public, len(data))
        return public

    def prepare_urls(self, urls: Iterable[str], *, crop: CropMode = "feed") -> List[str]:
        out: List[str] = []
        for url in urls:
            out.append(self.prepare_url(url, crop=crop))
        return out

    def prepare_postiz_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Rewrite Postiz `posts[].value[].image[].path` to Instagram-safe public URLs."""
        rewritten = copy.deepcopy(payload)
        posts = rewritten.get("posts")
        if not isinstance(posts, list):
            return rewritten

        for post in posts:
            if not isinstance(post, dict):
                continue
            settings = post.get("settings") if isinstance(post.get("settings"), dict) else {}
            type_name = str(settings.get("__type") or "").strip().lower()
            is_ig = type_name in INSTAGRAM_TYPES
            is_tiktok = type_name == "tiktok"
            is_story = str(settings.get("post_type") or "").strip().lower() == "story"
            crop: CropMode = "story" if (is_ig and is_story) else ("feed" if is_ig else "none")

            value = post.get("value")
            if not isinstance(value, list):
                continue
            for block in value:
                if not isinstance(block, dict):
                    continue
                images = block.get("image")
                if isinstance(images, dict):
                    images = [images]
                if not isinstance(images, list):
                    continue
                items = [x for x in images if isinstance(x, dict) and str(x.get("path") or "").strip()]
                paths = [str(x.get("path")).strip() for x in items]
                if is_ig:
                    paths = select_instagram_media_urls(paths, is_story=is_story)
                    by_path = {str(x.get("path")).strip(): x for x in items}
                    items = [by_path[p] for p in paths if p in by_path]
                prepared_items: List[Dict[str, Any]] = []
                for index, item in enumerate(items):
                    original = str(item.get("path") or "").strip()
                    if not original.lower().startswith(("http://", "https://")):
                        raise InstagramMediaPrepareError(
                            "Upload the file again before posting. This media link is only on this device."
                        )
                    new_path = self.prepare_url(original, crop=crop, require_video=is_tiktok)
                    prepared_items.append(
                        {
                            "id": item.get("id") or f"media_{index}",
                            "path": new_path,
                        }
                    )
                block["image"] = prepared_items
        return rewritten
