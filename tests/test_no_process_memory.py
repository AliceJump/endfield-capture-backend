"""底线守卫：这个项目**只**看网卡与磁盘，不碰游戏进程。

边界写在 README 与各模块 docstring 里，但文档拦不住手滑。这里做源码级检查：``src/`` 的
**代码**里不许出现读进程内存 / 附加进程 / 注入 / Hook 的 API —— 注释与文档字符串不算，
因为它们本来就要讲这件事。

允许的相近名字只有 unicorn 的 ``UC_HOOK_*``：那是**我们自己模拟器**的指令/内存回调，
跑的是从 DLL 文件读进来的字节，和"Hook 游戏"没有关系。
"""

from __future__ import annotations

import io
import tokenize
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

#: 出现任何一个都视为越过了项目的底线
BANNED = (
    "ReadProcessMemory",
    "WriteProcessMemory",
    "VirtualQueryEx",
    "VirtualAllocEx",
    "CreateRemoteThread",
    "OpenProcess",
    "OpenThread",
    "DebugActiveProcess",
    "SetWindowsHookEx",
    "pymem",
    "frida",
)

#: 注：不禁止"调用 Win32"本身——命名管道（``ctypes.WinDLL("kernel32")``）是合法的。
#: 禁的是**进程与内存**那一类：读别人内存、附加、注入、Hook。


def _code_only(source: str) -> str:
    """丢掉注释与字符串，只留真正的代码 token。"""

    pieces = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        pieces.append(token.string)
    return " ".join(pieces)


class ProcessMemoryBoundaryTest(unittest.TestCase):
    def test_src_never_touches_another_process(self):
        offenders = []
        for path in sorted(SRC.rglob("*.py")):
            code = _code_only(path.read_text(encoding="utf-8"))
            found = [name for name in BANNED if name in code]
            if found:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {', '.join(found)}")
        self.assertEqual(offenders, [], "项目底线是不能读内存／附加进程，别把这些 API 加回来")

    def test_the_guard_actually_catches_something(self):
        """守卫本身要有效：喂一段真会越界的代码，必须能认出来。"""

        sample = "\n".join(
            [
                '"""读内存的示例（字符串里出现不算）"""',
                "import ctypes  # ReadProcessMemory 在注释里也不算",
                "handle = ctypes.windll.kernel32.OpenProcess(0x10, False, pid)",
                "ctypes.windll.kernel32.ReadProcessMemory(handle, 0, buf, 8, None)",
            ]
        )
        code = _code_only(sample)
        self.assertIn("OpenProcess", code)
        self.assertIn("ReadProcessMemory", code)

    def test_win32_pipe_usage_is_allowed(self):
        """命名管道要调 Win32，但那是文件对象，不是进程内存——不该被守卫误伤。"""

        code = _code_only('ctypes.WinDLL("kernel32").CreateNamedPipeW("x", 3, 0, 255, 0, 0, 0, None)')
        self.assertEqual([name for name in BANNED if name in code], [])


if __name__ == "__main__":
    unittest.main()
