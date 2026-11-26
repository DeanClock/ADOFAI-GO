"""生成一个「UMM 用 Assembly 方式注入」的假游戏目录，供手工验收 UMM 页面。

UMM 0.33.x 的安装器在 Assembly 模式下 patch 的是运行时 Config.xml 里
EntryPoint 指向的程序集（ADOFAI 上是 ``UnityEngine.CoreModule.dll``），不是
StartingPoint 指向的 ``Assembly-CSharp.dll``；旧版 UMM 才可能 patch StartingPoint。
默认按 0.33.x 的真实结果生成。

假目录完全新建在系统临时目录下，只写自己创建的文件，不会碰真实游戏目录。
生成后按屏幕提示把 UMM 页面的「游戏目录」指过去即可。

用法（项目根目录下）：

    .venv\\Scripts\\python.exe tools\\make_fake_umm_assembly_env.py

可选参数：

    --target entry|starting
                     被 patch 的程序集。entry（默认）= UnityEngine.CoreModule.dll，
                     与 UMM 0.33.x 一致；starting = Assembly-CSharp.dll，模拟旧版。
    --original 0/1   是否同时生成 .original_ 备份（默认 1）。
                     设为 0 可验收「缺备份时拒绝卸载」的提示。
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

GAME_NAME = "A Dance of Fire and Ice"
STARTING_ASSEMBLY = "Assembly-CSharp.dll"
ENTRY_ASSEMBLY = "UnityEngine.CoreModule.dll"
MARKER = b"UnityModManagerNet.Injection.UnityModManagerStarter"
STARTING_ORIGINAL = b"MZ fake original Assembly-CSharp.dll payload\n"
ENTRY_ORIGINAL = b"MZ fake original UnityEngine.CoreModule.dll payload\n"
CONFIG_XML = """<?xml version="1.0" encoding="utf-8"?>
<Config Name="A Dance of Fire and Ice">
  <Folder>A Dance of Fire and Ice</Folder>
  <ModInfo>Info.json</ModInfo>
  <GameExe>A Dance of Fire and Ice.exe</GameExe>
  <EntryPoint>[UnityEngine.CoreModule.dll]UnityEngine.Application.cctor:Before</EntryPoint>
  <StartingPoint>[Assembly-CSharp.dll]ADOStartup.Startup:Before</StartingPoint>
  <UIStartingPoint>[Assembly-CSharp.dll]ADOStartup.Startup:After</UIStartingPoint>
</Config>
"""

TARGETS = {
    "entry": (ENTRY_ASSEMBLY, ENTRY_ORIGINAL),
    "starting": (STARTING_ASSEMBLY, STARTING_ORIGINAL),
}


def build(
    root: Path, *, with_backup: bool, target: str = "entry"
) -> tuple[Path, Path]:
    """生成假游戏目录，返回 (游戏目录, 被 patch 的程序集路径)。"""
    patched_name, original = TARGETS[target]
    game = root / GAME_NAME
    game.mkdir(parents=True)
    (game / f"{GAME_NAME}.exe").write_bytes(b"MZ")
    managed = game / f"{GAME_NAME}_Data" / "Managed"
    manager = managed / "UnityModManager"
    manager.mkdir(parents=True)
    (manager / "UnityModManager.dll").write_bytes(b"fake UnityModManager.dll")
    (manager / "Config.xml").write_text(CONFIG_XML, encoding="utf-8")
    # 真实 Managed 目录里两个候选程序集都在；只有 target 指定的那个被 patch。
    for name, payload in (
        (STARTING_ASSEMBLY, STARTING_ORIGINAL),
        (ENTRY_ASSEMBLY, ENTRY_ORIGINAL),
    ):
        (managed / name).write_bytes(payload)
    patched = managed / patched_name
    # 模拟打过补丁的程序集：原内容后面拼上 UMM 注入的类型名。
    patched.write_bytes(original + b"\0" + MARKER + b"\0")
    if with_backup:
        patched.with_name(patched.name + ".original_").write_bytes(original)
    return game, patched


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 UMM Assembly 注入的假游戏目录")
    parser.add_argument(
        "--target",
        choices=tuple(TARGETS),
        default="entry",
        help="被 patch 的程序集：entry=UnityEngine.CoreModule.dll（UMM 0.33.x，默认），"
        "starting=Assembly-CSharp.dll（旧版）",
    )
    parser.add_argument(
        "--original",
        type=int,
        choices=(0, 1),
        default=1,
        help="是否生成 .original_ 备份（1 是，0 否）",
    )
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="adofai-go-umm-assembly-"))
    game, entry = build(root, with_backup=bool(args.original), target=args.target)
    untouched = STARTING_ASSEMBLY if args.target == "entry" else ENTRY_ASSEMBLY
    untouched_path = game / f"{GAME_NAME}_Data" / "Managed" / untouched

    print("已生成假游戏目录（只写入了这个临时目录）：")
    print(game)
    print()
    print(f"模式：{'UMM 0.33.x（EntryPoint）' if args.target == 'entry' else '旧版（StartingPoint）'}")
    print("被 patch 的入口程序集：")
    print(entry)
    print("当前状态：含 UnityModManagerStarter 字节，模拟 UMM Assembly 注入。")
    if args.original:
        print(f"原版备份：{entry.name}.original_（卸载时会被还原）")
    else:
        print("注意：本次没有生成 .original_ 备份，卸载应被拒绝并给出提示。")
    print(f"对照：{untouched} 保持原版，卸载时不应被改动。")
    print()
    print("验收步骤：")
    print("1. 在项目根目录运行：")
    print("   .\\run.cmd --page umm")
    print("2. 在「游戏目录」一行点「选择目录」，选中上面的路径：")
    print(f"   {game}")
    print("3. 期望：「Unity Mod Manager」卡片状态显示「已安装 · <该路径>」，")
    print("   鼠标悬停状态文字时提示「UMM 已安装，Assembly 注入完成」。")
    print("4. 点「卸载 UMM」并确认：")
    if args.original:
        print(f"   - {entry.name} 内容恢复为原版（不含 UnityModManagerStarter），")
        print("     .original_ 被删除，Managed\\UnityModManager 目录被删除，状态变为「未安装」。")
    else:
        print("   - 弹出「找不到原文件备份」错误，目录保持原样，状态仍为「已安装」。")
    print(f"   - {untouched_path.name} 不应被改动。")
    print()
    print("验收结束后点「自动探测」即可把工具切回真实游戏目录。")
    print("这个临时目录可以随时整个删除，不影响真实游戏。")
    print(f"删除命令：Remove-Item -Recurse -Force \"{root}\"")


if __name__ == "__main__":
    main()
