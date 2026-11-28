"""仓库根 ``run.cmd`` 启动入口的测试。

只测 ``--print-command``：它打印最终会执行的命令后立即退出，不启动 GUI，
因此可以在 pytest 里安全运行。真实 GUI 启动由人工手动验收。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_CMD = REPO_ROOT / "run.cmd"

# cmd.exe 在自己的代码页里输出；run.cmd 只输出 ASCII，这里宽松解码即可。
_OUTPUT_ENCODING = "oem" if os.name == "nt" else "utf-8"

# 故意给一个不存在的平台名：万一脚本误启动 GUI，Qt 会立刻报错退出，
# 而不是弹窗等待，测试就能明确失败而不是挂住。
_NO_GUI_ENV = {**os.environ, "QT_QPA_PLATFORM": "run-cmd-test-no-gui"}

pytestmark = pytest.mark.skipif(os.name != "nt", reason="run.cmd 只在 Windows 上可用")


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["cmd", "/c", str(RUN_CMD), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding=_OUTPUT_ENCODING,
        errors="replace",
        timeout=120,
        env=_NO_GUI_ENV,
    )


def _require_interpreter(proc: subprocess.CompletedProcess[str]) -> None:
    """没有 .venv 的检出（例如干净的 CI 克隆）没有解释器可找，跳过即可。"""
    if proc.returncode != 0 and not (REPO_ROOT / ".venv").exists():
        pytest.skip("this checkout has no .venv; run.cmd has no interpreter to find")


def _quoted_paths(output: str) -> list[Path]:
    return [Path(item) for item in re.findall(r'"([^"]+)"', output)]


def test_print_command_reports_existing_python() -> None:
    proc = _run("--print-command", "--page", "workspace", "--tab", "4", cwd=REPO_ROOT)
    _require_interpreter(proc)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "run.py" in proc.stdout
    assert "--page workspace --tab 4" in proc.stdout

    paths = _quoted_paths(proc.stdout)
    assert len(paths) >= 2, proc.stdout
    python_path, script_path = paths[0], paths[1]
    assert python_path.name.lower().startswith("python")
    assert python_path.is_file(), f"interpreter does not exist: {python_path}"
    assert script_path.name.lower() == "run.py"
    assert script_path.is_file()


def test_print_command_does_not_launch_gui() -> None:
    started = time.monotonic()
    proc = _run("--print-command", "--page", "settings", cwd=REPO_ROOT)
    elapsed = time.monotonic() - started
    _require_interpreter(proc)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    # 只打印一行命令，并且不把开关本身传给 run.py。
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, proc.stdout
    assert "--print-command" not in proc.stdout
    assert elapsed < 60, f"--print-command took {elapsed:.1f}s, GUI may have started"


def test_print_command_works_from_another_directory(tmp_path: Path) -> None:
    proc = _run("--print-command", "--page", "workspace", "--tab", "4", cwd=tmp_path)
    _require_interpreter(proc)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    paths = _quoted_paths(proc.stdout)
    assert len(paths) >= 2, proc.stdout
    assert paths[1].name.lower() == "run.py"
    assert paths[1].is_file()


def test_reports_clear_error_when_no_python_is_usable(tmp_path: Path) -> None:
    """没有 .venv、pyvenv.cfg 指向不存在的 home 时，给出可读错误而不是静默失败。"""
    (tmp_path / "run.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text(
        "home = C:\\run-cmd-test-does-not-exist\\python\n", encoding="utf-8"
    )
    shutil.copy2(RUN_CMD, tmp_path / "run.cmd")

    proc = subprocess.run(
        ["cmd", "/c", str(tmp_path / "run.cmd"), "--print-command", "--page", "settings"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        encoding=_OUTPUT_ENCODING,
        errors="replace",
        timeout=120,
        env=_NO_GUI_ENV,
    )

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "No usable Python interpreter" in proc.stdout
    assert "pyvenv.cfg" in proc.stdout
