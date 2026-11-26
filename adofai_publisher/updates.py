from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import requests

from .self_update import RELEASE_FILE_ENV, release_asset_local_path

GITHUB_API = "https://api.github.com"
USER_AGENT = "ADOFAI-GO-Update-Checker"

_VERSION_RE = re.compile(
    r"^[vV]?(\d+(?:\.\d+)*)(?:-([0-9A-Za-z._-]+))?(?:\+[0-9A-Za-z._-]+)?$"
)
_REPO_RE = re.compile(r"^[0-9A-Za-z_.-]+/[0-9A-Za-z_.-]+$")


class UpdateError(RuntimeError):
    """更新检查失败，消息可直接展示给用户。"""


class UpdateNotConfigured(UpdateError):
    """尚未配置更新仓库。"""


def normalize_repo(value: str) -> str:
    repo = (value or "").strip()
    if not repo:
        raise UpdateNotConfigured("未配置更新仓库")
    if repo.startswith(("https://github.com/", "http://github.com/")):
        repo = repo.split("github.com/", 1)[1]
    repo = repo.strip().strip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not _REPO_RE.fullmatch(repo):
        raise UpdateError("仓库格式应为 owner/repo，例如 DeanColck/ADOFAI-GO")
    return repo


def parse_version(value: str) -> tuple[tuple[int, ...], tuple[str, ...]]:
    text = (value or "").strip()
    match = _VERSION_RE.fullmatch(text)
    if match is None:
        raise ValueError(f"无法识别的版本号：{value}")
    release = tuple(int(part) for part in match.group(1).split("."))
    prerelease = tuple(
        part for part in (match.group(2) or "").split(".") if part
    )
    return release, prerelease


def _compare_prerelease(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    if not left and not right:
        return 0
    if not left:
        return 1
    if not right:
        return -1
    for a, b in zip(left, right):
        if a == b:
            continue
        a_numeric = a.isdigit()
        b_numeric = b.isdigit()
        if a_numeric and b_numeric:
            return 1 if int(a) > int(b) else -1
        if a_numeric != b_numeric:
            return -1 if a_numeric else 1
        return 1 if a.lower() > b.lower() else -1
    if len(left) == len(right):
        return 0
    return 1 if len(left) > len(right) else -1


def compare_versions(left: str, right: str) -> int:
    left_release, left_pre = parse_version(left)
    right_release, right_pre = parse_version(right)
    width = max(len(left_release), len(right_release))
    left_padded = left_release + (0,) * (width - len(left_release))
    right_padded = right_release + (0,) * (width - len(right_release))
    if left_padded != right_padded:
        return 1 if left_padded > right_padded else -1
    return _compare_prerelease(left_pre, right_pre)


def is_newer(latest: str, current: str) -> bool:
    return compare_versions(latest, current) > 0


def _release_url(repo: str, payload: dict[str, Any]) -> str:
    url = str(payload.get("html_url") or "").strip()
    if url.startswith(("https://github.com/", "http://github.com/")):
        return url
    return f"https://github.com/{repo}/releases"


def _normalize_assets(
    payload: Any,
    *,
    local_base: Path | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    assets: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        raw_url = str(item.get("browser_download_url") or "")
        local = False
        if local_base is not None and raw_url:
            local_path = release_asset_local_path(local_base, raw_url)
            if local_path is not None:
                raw_url = str(local_path)
                local = True
        assets.append(
            {
                "name": str(item.get("name") or ""),
                "size": int(item.get("size") or 0),
                "url": raw_url,
                "local": local,
            }
        )
    return assets


def _load_release_override() -> tuple[dict[str, Any], Path] | None:
    """本地验收开关：``ADOFAI_UPDATE_RELEASE_FILE`` 指向一份 Release JSON。

    只在显式设置环境变量时生效，且打包版一律忽略——正式更新只走 GitHub，
    不会因为环境变量去读本地文件。离线验收靠它，配合 ``tools/update_demo.py``。
    """
    if getattr(sys, "frozen", False):
        return None
    raw = os.environ.get(RELEASE_FILE_ENV, "").strip()
    if not raw:
        return None
    path = Path(raw)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UpdateError(f"本地测试更新文件读取失败：{path}") from exc
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise UpdateError(f"本地测试更新文件不是有效 JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise UpdateError("本地测试更新文件格式不正确：顶层应为对象")
    return payload, path


def check_for_update(
    repo: str,
    current_version: str,
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    normalized = normalize_repo(repo)
    override = _load_release_override()
    local_base: Path | None = None
    if override is not None:
        payload, release_file = override
        local_base = release_file.parent
    else:
        url = f"{GITHUB_API}/repos/{normalized}/releases/latest"
        try:
            response = requests.get(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": USER_AGENT,
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=timeout,
            )
        except requests.Timeout as exc:
            raise UpdateError("网络请求超时，请检查网络后重试") from exc
        except requests.RequestException as exc:
            message = str(exc).splitlines()[0][:160]
            raise UpdateError(f"网络不通，无法连接 GitHub：{message}") from exc

        if response.status_code == 403:
            raise UpdateError("GitHub 限流（403），请稍后再试")
        if response.status_code == 404:
            raise UpdateError("仓库不存在或没有可用的 Release（404），请检查 owner/repo")
        if response.status_code != 200:
            raise UpdateError(f"GitHub 返回 HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise UpdateError("更新信息解析失败：GitHub 返回的不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise UpdateError("更新信息解析失败：响应格式不正确")

    tag = str(payload.get("tag_name") or "").strip()
    if not tag:
        raise UpdateError("更新信息解析失败：缺少版本号 tag_name")
    try:
        newer = is_newer(tag, current_version)
    except ValueError as exc:
        raise UpdateError(f"更新信息解析失败：{exc}") from exc

    return {
        "repo": normalized,
        "current": current_version,
        "latest": tag,
        "has_update": newer,
        "body": str(payload.get("body") or ""),
        "html_url": _release_url(normalized, payload),
        "assets": _normalize_assets(payload.get("assets"), local_base=local_base),
    }
