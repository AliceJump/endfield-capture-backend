"""PyInstaller 入口：等价于 ``endfield-capture-backend`` 命令行。"""

from __future__ import annotations

import multiprocessing
import sys

from endfield_capture.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()  # 打包后 multiprocessing 才不会重复起进程
    sys.exit(main())
