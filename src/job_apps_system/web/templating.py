from __future__ import annotations

from functools import lru_cache
from hashlib import sha256
from pathlib import Path

from fastapi.templating import Jinja2Templates


WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"
TEMPLATES_DIR = WEB_DIR / "templates"


@lru_cache(maxsize=1)
def static_asset_version() -> str:
    """Return one cache key for the whole static bundle.

    The WebView can keep old CSS/JS aggressively. A single app-wide fingerprint
    keeps all assets moving together so rebuilt HTML cannot mix new JS with old CSS.
    """
    digest = sha256()
    for path in sorted(STATIC_DIR.rglob("*")):
        if not path.is_file():
            continue
        relative_path = path.relative_to(STATIC_DIR).as_posix()
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def static_url(asset_path: str) -> str:
    normalized = asset_path.strip().lstrip("/")
    if normalized.startswith("static/"):
        normalized = normalized.removeprefix("static/")
    return f"/static/{normalized}?v={static_asset_version()}"


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["static_url"] = static_url
