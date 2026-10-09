"""入口点（`__main__` 块）冒烟：静态校验 + 真实启动（Issue #14）。

**为什么需要**：pytest 从不执行 `if __name__ == "__main__":` 块，所以入口里的
错误在测试集里**完全隐形**。Issue #14 就踩了这个：`register_catchups()` 被调用在
其定义**之前** → `NameError` → 容器启动即崩、`restart: unless-stopped` 反复拉起
（crash loop），而全套单测全绿。

两道防线：
  1. **静态**：`__main__` 块里调用到的模块级函数，定义必须出现在它之前（模块体顺序执行）；
  2. **动态**：真的把 `python -m app.tasks` 起起来，断言打印出 "Scheduler started"
     （不触网、不连库——APScheduler 用 MemoryJobStore，健康端点只在本地监听）。
"""
import ast
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
TASKS_PY = APP_DIR / "app" / "tasks.py"


def _main_block(tree):
    for node in tree.body:
        if (isinstance(node, ast.If)
                and getattr(getattr(node.test, "left", None), "id", None) == "__name__"):
            return node
    return None


def test_all_functions_called_in_main_are_defined_before_it():
    """静态守卫：`__main__` 里调用的模块级函数不得定义在其后（顺序执行的 NameError）"""
    tree = ast.parse(TASKS_PY.read_text(encoding="utf-8"))
    main = _main_block(tree)
    assert main is not None, "未找到 __main__ 块"

    defs = {n.name: n.lineno for n in tree.body if isinstance(n, ast.FunctionDef)}
    called = {n.func.id for n in ast.walk(main)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    offenders = sorted(c for c in called if c in defs and defs[c] > main.lineno)
    assert not offenders, (
        f"{TASKS_PY.name} 的 __main__ 块调用了在其后才定义的函数 {offenders} —— "
        "模块体顺序执行，会 NameError 使进程启动即崩（Issue #14 真实事故）")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_entrypoint_starts_scheduler():
    """动态守卫：真起 `python -m app.tasks`，须走到 "Scheduler started"。

    `BlockingScheduler.start()` 会**永久阻塞**（这是它的正常工作方式），所以不能等
    进程退出——流式读日志，见到目标行即判定成功并终止进程。
    """
    # 健康端口被占说明本地已有 collector 在跑 → 跳过，避免撞端口造成假失败
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 9200))
    except OSError:
        pytest.skip("9200 已被占用（本地已有 collector 在跑），跳过入口冒烟")
    finally:
        probe.close()

    proc = subprocess.Popen(
        [sys.executable, "-m", "app.tasks"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=str(APP_DIR), bufsize=1)

    lines: list[str] = []
    ok = False
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break  # 进程已退出（崩溃）→ 不 ok
            lines.append(line)
            if "Scheduler started" in line:
                ok = True
                break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    tail = "".join(lines[-40:])
    assert ok, f"入口未能启动调度器：\n{tail}"
    assert "NameError" not in tail and "Traceback" not in tail, \
        f"入口启动过程抛异常：\n{tail}"
