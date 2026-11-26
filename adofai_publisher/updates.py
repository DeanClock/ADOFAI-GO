from __future__ import annotations

import re
from typing import Any

import requests

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


def _normalize_assets(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    assets: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        assets.append(
            {
                "name": str(item.get("name") or ""),
                "size": int(item.get("size") or 0),
                "url": str(item.get("browser_download_url") or ""),
            }
        )
    return assets


def check_for_update(
    repo: str,
    current_version: str,
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    normalized = normalize_repo(repo)
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
        "assets": _normalize_assets(payload.get("assets")),
    }
