"""构造离线「应用内更新」验收环境（不联网、不碰真实 exe）。

用法::

    python tools/update_demo.py          # 生成演示文件并打印验收步骤
    python tools/update_demo.py --check  # 退出应用后检查 target.exe 是否被替换

生成内容（默认在 tools/update-demo/，已被 .gitignore 忽略）：

    ADOFAI_GO_v9.9.9.exe        假的「新版本安装包」
    ADOFAI_GO_v9.9.9.exe.sha256 对应的 SHA256 校验文件
    release.json                本地 Release 描述，供 ADOFAI_UPDATE_RELEASE_FILE
    target.exe                  假的「当前 exe」，退出应用后由 helper 替换
    target.exe.old              替换成功后出现的旧版本备份

注意：``ADOFAI_UPDATE_INSTALL_TARGET`` 只用于源码验收，务必指向这个
演示目录里的 target.exe，不要指向真实的 ADOFAI GO exe。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

UPDATE_NAME = "ADOFAI_GO_v9.9.9.exe"
CHECKSUM_NAME = UPDATE_NAME + ".sha256"
TARGET_NAME = "target.exe"
RELEASE_NAME = "release.json"
OLD_TARGET_BYTES = b"OLD ADOFAI GO demo target - safe to replace\n"
RELEASES_URL = "https://github.com/DeanClock/ADOFAI-GO/releases"


def _payload(size_bytes: int) -> bytes:
    line = b"ADOFAI GO demo update payload\n"
    repeats = max(1, size_bytes // len(line) + 1)
    return (line * repeats)[: max(1, size_bytes)]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def generate(out: Path, size_mb: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    update = _payload(max(1, size_mb) * 1024 * 1024)
    digest = _sha256(update)
    (out / UPDATE_NAME).write_bytes(update)
    (out / CHECKSUM_NAME).write_text(
        f"{digest}  {UPDATE_NAME}\n", encoding="utf-8"
    )
    release = {
        "tag_name": "v9.9.9",
        "body": (
            "离线验收入口：应用内下载更新包、校验 SHA256，"
            "关闭应用后由独立 helper 替换 target.exe。"
        ),
        "html_url": RELEASES_URL,
        "assets": [
            {
                "name": UPDATE_NAME,
                "size": len(update),
                "browser_download_url": UPDATE_NAME,
            },
            {
                "name": CHECKSUM_NAME,
                "size": (out / CHECKSUM_NAME).stat().st_size,
                "browser_download_url": CHECKSUM_NAME,
            },
        ],
    }
    (out / RELEASE_NAME).write_text(
        json.dumps(release, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 每次生成都把 target.exe 复位成「旧版本」，方便反复验收。
    (out / TARGET_NAME).write_bytes(OLD_TARGET_BYTES)
    old = out / (TARGET_NAME + ".old")
    if old.exists():
        old.unlink()


def check(out: Path) -> int:
    update = out / UPDATE_NAME
    target = out / TARGET_NAME
    if not update.is_file() or not target.is_file():
        print(f"没有找到演示文件：{out}")
        print("先运行 python tools/update_demo.py 生成。")
        return 2
    expected = _sha256(update.read_bytes())
    actual = _sha256(target.read_bytes())
    print(f"新版本 SHA256：{expected}")
    print(f"target.exe SHA256：{actual}")
    if actual == expected:
        backup = out / (TARGET_NAME + ".old")
        print("替换成功：target.exe 已经是新版本内容。")
        if backup.is_file():
            print(f"旧版本备份仍在：{backup.name}")
        return 0
    if target.read_bytes() == OLD_TARGET_BYTES:
        print("还没替换：target.exe 仍是旧内容。")
        print("确认已点「下载并安装更新」、看到「退出后自动替换」，并已关闭应用。")
        return 1
    print("target.exe 内容既不是旧版本也不是新版本，请检查替换日志。")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="ADOFAI GO 应用内更新离线验收工具")
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent / "update-demo"),
        help="演示文件输出目录（默认 tools/update-demo/）",
    )
    parser.add_argument(
        "--size-mb",
        type=int,
        default=8,
        help="假安装包大小（MB，默认 8）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查 target.exe 是否已被替换",
    )
    args = parser.parse_args()
    out = Path(args.out).resolve()
    if args.check:
        return check(out)

    generate(out, args.size_mb)
    release = out / RELEASE_NAME
    target = out / TARGET_NAME
    print(f"演示文件已生成：{out}")
    print()
    print("在 PowerShell 里设置两个环境变量，然后启动应用：")
    print()
    print(f'  $env:ADOFAI_UPDATE_RELEASE_FILE="{release}"')
    print(f'  $env:ADOFAI_UPDATE_INSTALL_TARGET="{target}"')
    print("  run.cmd --page settings")
    print()
    print("验收步骤：")
    print("  1. 设置页「软件更新」点「检查更新」，应显示「发现新版本 v9.9.9」。")
    print("  2. 点「下载并安装更新」，应看到进度和「正在校验 SHA256…」。")
    print("  3. 完成后显示「更新包已下载并通过 SHA256 校验，退出应用后自动替换」。")
    print("  4. 关闭主窗口，等 2~3 秒后运行：")
    print("       python tools/update_demo.py --check")
    print("     应显示「替换成功」，target.exe.old 是旧版本备份。")
    print()
    print("提示：环境变量只对当前终端生效；它只影响源码验收，")
    print("打包版永远替换自己的 exe。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
