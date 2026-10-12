"""Error codes of a publication attempt (ТЗ §20) and whether a retry is safe."""
from __future__ import annotations


class PublishError(RuntimeError):
    def __init__(self, code: str, message: str, retry: bool = True, after_share: bool = False):
        super().__init__(message)
        self.code, self.retry, self.after_share = code, retry, after_share


class NeedsPerson(PublishError):
    """CAPTCHA, login confirmation, security check: the queue stops until a person acts in Chrome."""
    def __init__(self, message: str):
        super().__init__("WAITING_USER", message, retry=False)


AUTH_REQUIRED, UPLOAD_FAILED, VIDEO_PROCESSING_TIMEOUT = "AUTH_REQUIRED", "UPLOAD_FAILED", "VIDEO_PROCESSING_TIMEOUT"
CAPTION_FAILED, COVER_FAILED, PUBLISH_FAILED = "CAPTION_FAILED", "COVER_FAILED", "PUBLISH_FAILED"
SESSION_EXPIRED, PAGE_CHANGED, UNKNOWN_ERROR, WRONG_ACCOUNT = "SESSION_EXPIRED", "PAGE_CHANGED", "UNKNOWN_ERROR", "WRONG_ACCOUNT"
STOPPED = "STOPPED"
CROP_FAILED = "CROP_FAILED"
