from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

from .config import Settings, base_dir

logger = logging.getLogger(__name__)

ADOFAI_TOOLS_BASE = "https://www.adofaitools.top"
ADOFAI_TOOLS_LOGIN_URL = f"{ADOFAI_TOOLS_BASE}/#/auth/login"
ADOFAI_TOOLS_MODS_URL = f"{ADOFAI_TOOLS_BASE}/#/mods?resourceType=MOD"

RESOURCE_TYPES = {
    "MOD": "模组",
    "RESOURCEPACK": "资源包",
    "SHADER": "着色器",
    "PLUGIN": "插件",
    "LIBRARY": "库",
    "TOOL": "工具",
    "OTHER": "其他",
}


class AdofaiToolsError(Exception):
    """ADOFAI Tools 请求失败。"""


def token_path() -> Path:
    return base_dir() / "adofaitools_token.json"


def load_token(settings: Settings | None = None) -> str:
    configured = ""
    if settings is not None:
        configured = str(settings.get("adofaitools_token") or "").strip()
    if configured:
        if not token_expired(configured):
            return configured
        clear_token()
        settings.set("adofaitools_token", "")
        try:
            settings.save()
        except OSError:
            pass
        return ""
    path = token_path()
    if not path.is_file():
        return ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(raw, dict):
        return ""
    token = str(raw.get("access_token") or "").strip()
    if token and token_expired(token):
        clear_token()
        return ""
    return token


def token_expired(token: str, *, leeway: int = 60) -> bool:
    """JWT 过期判断；无法解析时视为未过期，交给服务端判定。"""
    parts = (token or "").split(".")
    if len(parts) < 2:
        return False
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        raw = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")))
    except Exception:
        return False
    exp = raw.get("exp") if isinstance(raw, dict) else None
    if not isinstance(exp, (int, float)):
        return False
    return float(exp) <= time.time() + leeway


def save_token(token: str) -> None:
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"access_token": token.strip()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def clear_token() -> None:
    try:
        token_path().unlink(missing_ok=True)
    except OSError:
        pass


def _headers(token: str) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "ADOFAI-GO",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get(path: str, token: str, params: dict[str, Any] | None = None) -> Any:
    import requests

    try:
        response = requests.get(
            ADOFAI_TOOLS_BASE + path,
            headers=_headers(token),
            params=params or {},
            timeout=25.0,
        )
    except Exception as exc:
        raise AdofaiToolsError(f"无法连接 ADOFAI Tools：{exc}") from exc
    if response.status_code == 401:
        raise AdofaiToolsError("登录已失效，请重新登录 ADOFAI Tools")
    if response.status_code >= 400:
        raise AdofaiToolsError(f"请求失败（HTTP {response.status_code}）")
    try:
        return response.json()
    except ValueError as exc:
        raise AdofaiToolsError("返回内容不是合法 JSON") from exc


def list_mods(
    token: str,
    *,
    page: int = 1,
    page_size: int = 100,
    keyword: str = "",
    resource_type: str = "MOD",
) -> dict[str, Any]:
    params: dict[str, Any] = {"page": page, "pageSize": page_size}
    if keyword:
        params["keyword"] = keyword
    if resource_type:
        params["resourceType"] = resource_type
    payload = _get("/api/mods", token, params)
    return normalize_list(payload)


def normalize_list(payload: Any) -> dict[str, Any]:
    data = payload
    if isinstance(payload, dict):
        data = payload.get("data", payload)
    items: list[dict[str, Any]] = []
    total = 0
    if isinstance(data, dict):
        raw_items = data.get("items") or data.get("list") or data.get("records") or []
        total = int(data.get("total") or data.get("count") or 0)
        if isinstance(raw_items, list):
            items = [normalize_mod(item) for item in raw_items if isinstance(item, dict)]
        elif isinstance(data.get("data"), list):
            items = [normalize_mod(item) for item in data["data"] if isinstance(item, dict)]
    elif isinstance(data, list):
        items = [normalize_mod(item) for item in data if isinstance(item, dict)]
        total = len(items)
    return {"items": items, "total": total or len(items)}


def normalize_mod(raw: dict[str, Any]) -> dict[str, Any]:
    versions = raw.get("versions") or []
    latest = raw.get("latestVersion")
    latest_version = ""
    download_url = ""
    file_name = ""
    version_id = ""
    if isinstance(latest, dict):
        latest_version = str(latest.get("versionId") or latest.get("version") or "")
        version_id = str(latest.get("versionId") or "")
        version_pk = str(latest.get("id") or "")
        chosen = next(
            (
                item
                for item in versions
                if isinstance(item, dict)
                and (str(item.get("id") or "") == version_pk
                     or str(item.get("versionId") or "") == version_id)
            ),
            None,
        )
        if chosen is not None:
            download_url, file_name = _pick_file(chosen)
    if not download_url and isinstance(versions, list):
        for item in versions:
            if not isinstance(item, dict):
                continue
            download_url, file_name = _pick_file(item)
            if download_url:
                latest_version = latest_version or str(item.get("versionId") or "")
                break
    return {
        "id": str(raw.get("id") or ""),
        "slug": str(raw.get("slug") or ""),
        "display_name": str(raw.get("displayName") or raw.get("name") or ""),
        "summary": str(raw.get("summary") or ""),
        "resource_type": str(raw.get("resourceType") or "MOD"),
        "version": latest_version,
        "downloads": int(raw.get("downloadCount") or raw.get("downloads") or 0),
        "updated_at": str(raw.get("updatedAt") or raw.get("publishedAt") or ""),
        "icon_url": str(raw.get("iconUrl") or ""),
        "download_url": download_url,
        "file_name": file_name,
        "raw": raw,
    }


def _pick_file(version: dict[str, Any]) -> tuple[str, str]:
    files = version.get("files") or []
    for item in files:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "") not in {"", "READY"}:
            continue
        url = str(item.get("downloadUrl") or item.get("sourceUrl") or item.get("url") or "")
        if url:
            return url, str(item.get("name") or "")
    return "", ""


def mod_detail(token: str, resource_type: str, slug: str) -> dict[str, Any]:
    payload = _get(f"/api/mods/{resource_type}/{slug}", token)
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    return data if isinstance(data, dict) else {}


def download_mod_file(
    token: str,
    resource_type: str,
    slug: str,
    download_url: str,
    dest_dir: Path,
    *,
    file_name: str = "",
    progress=None,
) -> Path:
    import requests

    dest_dir.mkdir(parents=True, exist_ok=True)
    url = download_url or ""
    if url.startswith("/"):
        url = ADOFAI_TOOLS_BASE + url
    if not url:
        raise AdofaiToolsError("该资源没有可下载的文件")
    target = dest_dir / (file_name or f"{slug}.zip")
    if not target.name.lower().endswith((".zip", ".adozip", ".dll")):
        target = dest_dir / f"{slug}.zip"
    received, total = _download_with_resume(url, token, target, progress=progress)
    if total and received < total:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise AdofaiToolsError("下载不完整，请重试")
    logger.info("ADOFAI Tools 下载完成：%s", target)
    return target


def _download_with_resume(
    url: str,
    token: str,
    target: Path,
    *,
    progress=None,
    max_attempts: int = 40,
) -> tuple[int, int]:
    """分段续传下载：服务器会中途断流，用 Range 从断点继续直到拿全。"""
    import requests

    received = target.stat().st_size if target.is_file() else 0
    total = 0
    attempts = 0
    while attempts < max_attempts:
        attempts += 1
        headers = _headers(token)
        headers["Accept-Encoding"] = "identity"
        if received:
            headers["Range"] = f"bytes={received}-"
        try:
            response = requests.get(
                url,
                headers=headers,
                timeout=120.0,
                stream=True,
                allow_redirects=True,
            )
        except Exception:
            if attempts >= max_attempts:
                raise AdofaiToolsError("下载失败：网络连接反复中断，请稍后重试")
            continue
        try:
            if response.status_code == 401:
                raise AdofaiToolsError("登录已失效，请重新登录 ADOFAI Tools")
            if response.status_code == 416 and received:
                content_range = response.headers.get("content-range") or ""
                total = _total_from_content_range(content_range) or received
                break
            if response.status_code >= 400:
                raise AdofaiToolsError(f"下载失败（HTTP {response.status_code}）")
            if received and response.status_code != 206:
                received = 0
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
            if not total:
                content_range = response.headers.get("content-range") or ""
                total = _total_from_content_range(content_range)
                if not total:
                    try:
                        total = int(response.headers.get("content-length") or 0)
                    except (TypeError, ValueError):
                        total = 0
            mode = "ab" if received else "wb"
            interrupted = False
            with target.open(mode) as out:
                try:
                    for chunk in response.iter_content(chunk_size=131072):
                        if not chunk:
                            continue
                        out.write(chunk)
                        received += len(chunk)
                        if progress is not None and total:
                            progress(received, total)
                except Exception:
                    interrupted = True
        finally:
            try:
                response.close()
            except Exception:
                pass
        if total and received >= total:
            break
        if not interrupted and total and received < total:
            interrupted = True
        if not interrupted:
            break
    return received, total


def _total_from_content_range(value: str) -> int:
    marker = value.rsplit("/", 1)
    if len(marker) == 2:
        try:
            return int(marker[1])
        except (TypeError, ValueError):
            return 0
    return 0
