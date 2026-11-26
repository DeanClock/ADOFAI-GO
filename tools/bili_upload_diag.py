#!/usr/bin/env python3
"""诊断 B 站投稿页的真实 DOM 结构（只读，不会点提交、不读取 cookie）。

用法：

    python tools/bili_upload_diag.py --settings "…/ADOFAI GO 数据/settings.json"

    # 也可以显式指定持久化 profile 与浏览器通道
    python tools/bili_upload_diag.py --profile "…/ADOFAI GO 数据/browser_profile" --channel msedge

脚本会打开一个可见浏览器窗口，读取投稿页（URL 默认取代码里的 BILI_UPLOAD_URL），
把按钮、输入框、关键区域、iframe / shadow DOM、以及现有选择器的命中数打印出来，
并可选写出 JSON。脚本只读取页面，不点击「立即投稿」，不打印任何 cookie / token。

退出码：0 正常；2 未能打开页面或未登录；3 页面结构未拿到。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adofai_publisher.browser import (  # noqa: E402
    BILI_UPLOAD_URL,
    has_bili_login,
    launch_context,
    redact_secrets,
)
from adofai_publisher.config import Settings  # noqa: E402
from adofai_publisher.workers import (  # noqa: E402
    DECLARATION_SELECTORS,
    TAG_CHIP_SELECTORS,
    TAG_INPUT_SELECTORS,
)

DUMP_JS = r"""
() => {
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const describe = (el) => ({
    tag: el.tagName.toLowerCase(),
    text: clean(el.innerText || el.textContent || '').slice(0, 100),
    type: el.getAttribute('type') || '',
    accept: el.getAttribute('accept') || '',
    multiple: el.hasAttribute('multiple'),
    placeholder: el.getAttribute('placeholder') || '',
    ariaLabel: el.getAttribute('aria-label') || '',
    role: el.getAttribute('role') || '',
    contenteditable: el.getAttribute('contenteditable') || '',
    name: el.getAttribute('name') || '',
    id: el.id || '',
    className: clean(el.className || '').slice(0, 240),
    disabled: !!el.disabled,
    parentText: clean(el.parentElement ? el.parentElement.innerText : '').slice(0, 100),
    parentClass: clean(el.parentElement ? el.parentElement.className : '').slice(0, 200),
  });

  const buttons = [];
  document
    .querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"], a')
    .forEach((el) => {
      if (!visible(el)) return;
      const item = describe(el);
      if (!item.text && !item.ariaLabel) return;
      buttons.push(item);
    });

  const fields = [];
  document
    .querySelectorAll('input, textarea, [contenteditable="true"], [contenteditable=""]')
    .forEach((el) => {
      if (!visible(el)) return;
      fields.push(describe(el));
    });

  const keywordAreas = {};
  const keywords = ['标签', '创作声明', '分区', '封面', '标题', '简介', '立即投稿', '发布', '稿件'];
  const nodes = Array.from(document.querySelectorAll('div, span, label, section, form'));
  for (const keyword of keywords) {
    const hits = [];
    for (const el of nodes) {
      const text = clean(el.innerText || '');
      if (!text.includes(keyword)) continue;
      if (text.length > 300) continue;  // 整页容器，跳过
      if (!visible(el)) continue;
      hits.push({
        tag: el.tagName.toLowerCase(),
        className: clean(el.className || '').slice(0, 200),
        role: el.getAttribute('role') || '',
        text: text.slice(0, 140),
        html: el.outerHTML.slice(0, 1500),
      });
      if (hits.length >= 6) break;
    }
    keywordAreas[keyword] = hits;
  }

  const shadows = [];
  const walk = (root, path) => {
    root.querySelectorAll('*').forEach((el) => {
      if (!el.shadowRoot) return;
      const label = el.tagName.toLowerCase() + (el.id ? '#' + el.id : '');
      shadows.push({
        host: label,
        hostClass: clean(el.className || '').slice(0, 200),
        html: el.shadowRoot.innerHTML.slice(0, 1000),
      });
      if (shadows.length < 20) walk(el.shadowRoot, path + ' > ' + label);
    });
  };
  walk(document, '');

  const frames = Array.from(document.querySelectorAll('iframe')).map((el) => ({
    src: el.getAttribute('src') || '',
    id: el.id || '',
    className: clean(el.className || '').slice(0, 160),
  }));

  return {
    title: document.title,
    url: location.href,
    bodyLength: (document.body.innerText || '').length,
    bodyText: clean(document.body.innerText || '').slice(0, 1200),
    links: Array.from(document.querySelectorAll('a[href]'))
      .map((el) => ({text: clean(el.innerText).slice(0, 40), href: el.getAttribute('href') || ''}))
      .filter((item) => /upload|edit|video/i.test(item.href))
      .slice(0, 30),
    buttons,
    fields,
    keywordAreas,
    shadows,
    iframes: frames,
  };
}
"""

TITLE_SELECTORS = (
    'input[placeholder*="标题"]',
    'input[placeholder*="请输入稿件标题"]',
    ".video-title input",
    "input.input-val",
    '[contenteditable="true"]',
    '[class*="title"] input',
    'input[maxlength="80"]',
    'textarea[placeholder*="标题"]',
)
SUBMIT_SELECTORS = (
    ".submit-button-anchor .submit-add",
    ".submit-add",
    'span:has-text("立即投稿")',
    'button:has-text("立即投稿")',
    'button:has-text("确认投稿")',
    'button:has-text("发布")',
    'div[role="button"]:has-text("立即投稿")',
    '[class*="submit"]',
    '[class*="publish"]',
)


def selector_probe(frame: Any) -> dict[str, list[dict[str, Any]]]:
    """在当前 frame 里试跑代码里用到的和备选的选择器，看命中几个。"""

    groups: dict[str, tuple[str, ...]] = {
        "title": TITLE_SELECTORS,
        "tags_input": TAG_INPUT_SELECTORS,
        "tags_chip": TAG_CHIP_SELECTORS,
        "declaration": DECLARATION_SELECTORS,
        "submit": SUBMIT_SELECTORS,
    }
    result: dict[str, list[dict[str, Any]]] = {}
    for name, selectors in groups.items():
        rows: list[dict[str, Any]] = []
        for selector in selectors:
            try:
                locator = frame.locator(selector)
                count = int(locator.count())
                visible_count = 0
                for index in range(min(count, 5)):
                    try:
                        if locator.nth(index).is_visible():
                            visible_count += 1
                    except Exception:
                        continue
                rows.append(
                    {"selector": selector, "count": count, "visible": visible_count}
                )
            except Exception as exc:
                rows.append(
                    {"selector": selector, "error": str(exc).splitlines()[0][:160]}
                )
        result[name] = rows
    return result


def dump_frame(frame: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"name": frame.name, "url": frame.url}
    try:
        data["dom"] = frame.evaluate(DUMP_JS)
    except Exception as exc:
        data["dom_error"] = str(exc).splitlines()[0][:200]
    try:
        data["selectors"] = selector_probe(frame)
    except Exception as exc:
        data["selector_error"] = str(exc).splitlines()[0][:200]
    return data


SCAN_JS = r"""
async (markers) => {
  const urls = new Set();
  performance.getEntriesByType('resource').forEach((entry) => {
    if (/\.js(\?|$)/.test(entry.name)) urls.add(entry.name);
  });
  document.querySelectorAll('script[src]').forEach((el) => urls.add(el.src));
  const result = [];
  for (const url of Array.from(urls).slice(0, 40)) {
    try {
      const response = await fetch(url, {credentials: 'include'});
      const text = await response.text();
      const hits = [];
      for (const marker of markers) {
        let index = text.indexOf(marker);
        let count = 0;
        while (index >= 0 && count < 3) {
          hits.push({marker, snippet: text.slice(Math.max(0, index - 180), index + 240)});
          index = text.indexOf(marker, index + marker.length);
          count += 1;
        }
      }
      const routes = new Set();
      const routePattern = /["']\/?(?:platform\/upload|york\/[a-z-]*upload)[^"'\s]{0,60}/g;
      let match;
      while ((match = routePattern.exec(text)) !== null) routes.add(match[0]);
      if (hits.length || routes.size) {
        result.push({
          url,
          size: text.length,
          hits,
          routes: Array.from(routes).slice(0, 25),
        });
      }
    } catch (error) {
      result.push({url, error: String(error).slice(0, 160)});
    }
  }
  return result;
}
"""
JS_MARKERS = (
    "立即投稿",
    "创作声明",
    "内容无需标注",
    "tag-input",
    "video-title",
    "确认发布",
    "点击上传",
)


def probe_editor(
    page: Any, context: Any, video_path: str, wait_seconds: float
) -> dict[str, Any]:
    """选择一个视频文件让编辑器渲染，但拦掉所有写请求，保证不上传、不建稿。"""

    blocked: list[str] = []

    def handler(route: Any) -> None:
        request = route.request
        try:
            method = request.method.upper()
            url = request.url.split("?")[0]
        except Exception:
            route.abort()
            return
        if method in ("GET", "HEAD", "OPTIONS"):
            route.continue_()
            return
        blocked.append(redact_secrets(f"{method} {url}"))
        route.abort()

    page.route("**/*", handler)
    result: dict[str, Any] = {
        "video": video_path,
        "selected": False,
        "blocked": blocked,
        "pages": [],
    }
    try:
        inputs = page.locator('input[type="file"]')
        for index in range(min(int(inputs.count()), 6)):
            node = inputs.nth(index)
            accept = (node.get_attribute("accept") or "").lower()
            if ".mp4" in accept or "video" in accept:
                node.set_input_files(video_path)
                result["selected"] = True
                break
        page.wait_for_timeout(int(wait_seconds * 1000))
    except Exception as exc:
        result["error"] = redact_secrets(str(exc).splitlines()[0][:200])
    try:
        result["url_after"] = page.url
        result["pages"] = [{"url": item.url} for item in context.pages]
    except Exception:
        pass
    return result


def scan_js(page: Any) -> list[dict[str, Any]]:
    try:
        return page.evaluate(SCAN_JS, list(JS_MARKERS))
    except Exception as exc:
        return [{"error": str(exc).splitlines()[0][:200]}]


def summarize(payload: dict[str, Any]) -> None:
    print("=" * 78)
    print(f"页面：{payload.get('title', '')}")
    print(f"URL ：{payload.get('url', '')}")
    if payload.get("probe"):
        probe = payload["probe"]
        print(
            f"编辑器探测：视频={probe.get('video')} 已选择={probe.get('selected')} "
            f"拦截写请求 {len(probe.get('blocked') or [])} 个"
        )
        for item in (probe.get("blocked") or [])[:20]:
            print(f"  已拦截：{item}")
        if probe.get("error"):
            print(f"  错误：{probe['error']}")
    print("=" * 78)
    for frame in payload["frames"]:
        print()
        print(f"--- frame name={frame.get('name')!r} url={frame.get('url')}")
        dom = frame.get("dom")
        if not dom:
            print(f"    DOM 读取失败：{frame.get('dom_error')}")
            continue
        print(f"    title={dom.get('title')!r} bodyLength={dom.get('bodyLength')}")
        if dom.get("bodyText"):
            print(f"    正文（截断）：{dom['bodyText']!r}")
        if dom.get("links"):
            print("    含 upload/edit 的链接：")
            for item in dom["links"]:
                print(f"      - {item}")
        if dom.get("iframes"):
            print("    iframe：")
            for item in dom["iframes"]:
                print(f"      - {item}")
        print("    可见按钮：")
        for item in dom.get("buttons") or []:
            print(
                f"      <{item['tag']}> text={item['text']!r} "
                f"role={item['role']!r} class={item['className']!r} "
                f"id={item['id']!r} disabled={item['disabled']}"
            )
        print("    可见输入框：")
        for item in dom.get("fields") or []:
            print(
                f"      <{item['tag']}> type={item['type']!r} "
                f"placeholder={item['placeholder']!r} role={item['role']!r} "
                f"ce={item['contenteditable']!r} class={item['className']!r} "
                f"parentClass={item['parentClass']!r}"
            )
        print("    关键词区域（截断）：")
        for keyword, hits in (dom.get("keywordAreas") or {}).items():
            if not hits:
                continue
            print(f"      [{keyword}]")
            for hit in hits[:3]:
                print(
                    f"        <{hit['tag']}> class={hit['className']!r} "
                    f"role={hit['role']!r} text={hit['text']!r}"
                )
                print(f"        html: {hit['html']}")
        if dom.get("shadows"):
            print("    shadow DOM：")
            for item in dom["shadows"]:
                print(f"      host={item['host']} class={item['hostClass']!r}")
                print(f"      {item['html']}")
        print("    选择器命中：")
        for group, rows in (frame.get("selectors") or {}).items():
            print(f"      [{group}]")
            for row in rows:
                if "error" in row:
                    print(f"        {row['selector']!r} -> 错误 {row['error']}")
                else:
                    print(
                        f"        {row['selector']!r} -> 命中 {row['count']}，"
                        f"可见 {row['visible']}"
                    )
    if payload.get("js_scan"):
        print()
        print("--- JS 扫描")
        for item in payload["js_scan"]:
            if "error" in item:
                print(f"    {item.get('url')} -> {item['error']}")
                continue
            print(f"    {item['url']} (size={item['size']})")
            for route in item.get("routes") or []:
                print(f"      路由: {route}")
            for hit in (item.get("hits") or [])[:12]:
                print(f"      [{hit['marker']}] …{hit['snippet'][:320]}…")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读诊断 B 站投稿页结构")
    parser.add_argument(
        "--settings",
        default="",
        help="settings.json 路径；给了就按里面的浏览器通道/profile 启动",
    )
    parser.add_argument("--profile", default="", help="持久化浏览器 profile 目录")
    parser.add_argument("--channel", default="", help="浏览器通道，如 msedge / chrome")
    parser.add_argument("--url", default=BILI_UPLOAD_URL, help="要诊断的页面")
    parser.add_argument(
        "--wait", type=float, default=8.0, help="页面加载后等待多少秒再读取（默认 8）"
    )
    parser.add_argument("--output", default="", help="把结果写成 JSON 的路径")
    parser.add_argument(
        "--scan-js",
        action="store_true",
        help="另外扫描页面已加载的 JS，找编辑器路由与关键类名（只读）",
    )
    parser.add_argument(
        "--probe-editor",
        default="",
        help=(
            "给一个本地视频文件：只选择文件让编辑器渲染出来，同时拦截所有非 GET 请求，"
            "保证视频不会真的上传、也不会创建稿件（会打印被拦下的请求）"
        ),
    )
    args = parser.parse_args(argv)

    settings_path = Path(args.settings) if args.settings else None
    settings = Settings(settings_path)
    if args.profile:
        settings.set("browser_profile_dir", args.profile)
    elif settings_path is not None and not str(
        settings.get("browser_profile_dir") or ""
    ).strip():
        # settings.json 里没写 profile 时，按便携版的布局用同目录的 browser_profile
        settings.set(
            "browser_profile_dir", str(settings_path.parent / "browser_profile")
        )
    if args.channel:
        settings.set("browser_channel", args.channel)

    from playwright.sync_api import sync_playwright

    payload: dict[str, Any] = {"url": args.url, "frames": []}
    with sync_playwright() as pw:
        print(f"正在启动浏览器（profile={settings.get('browser_profile_dir') or '默认'}，"
              f"channel={settings.get('browser_channel') or '默认'}）…")
        try:
            context = launch_context(pw, settings, headless=False)
        except Exception as exc:
            print(f"浏览器启动失败：{redact_secrets(str(exc))}", file=sys.stderr)
            return 2
        try:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(args.url, wait_until="domcontentloaded", timeout=90000)
            except Exception as exc:
                print(f"打开页面失败：{redact_secrets(str(exc))}", file=sys.stderr)
                return 2
            try:
                page.wait_for_timeout(int(args.wait * 1000))
            except Exception:
                pass

            if args.probe_editor:
                payload["probe"] = probe_editor(
                    page, context, args.probe_editor, args.wait
                )


            if "passport" in page.url or not has_bili_login(context):
                print(
                    "B 站未登录：请在这个浏览器窗口里登录后重跑本脚本。",
                    file=sys.stderr,
                )
                payload["login"] = False
            else:
                payload["login"] = True

            payload["page_url"] = page.url
            try:
                targets = [item for item in context.pages if not item.is_closed()]
            except Exception:
                targets = [page]
            for target in targets:
                try:
                    frames = list(target.frames)
                except Exception:
                    continue
                for frame in frames:
                    payload["frames"].append(dump_frame(frame))
            if args.scan_js:
                payload["js_scan"] = scan_js(targets[-1] if targets else page)
        finally:
            try:
                context.close()
            except Exception:
                pass

    summarize(payload)
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n已写入 {target}")
    if not payload.get("login", True):
        return 2
    if not payload["frames"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
