"""参考会话回归：拿一份完整抓包跑一遍 ``selftest``。

``selftest.REFERENCE_EXPECTATION`` 里那组数值来自研究阶段的参考会话，能逐一核对才说明解码链
没坏。但**那份抓包不进仓库**——它是一次真实会话的完整流量，含登录握手、两端 IP 与精确时间，
放进公开历史就撤不回来了。

所以这份抓包要你自己放：默认读 ``tests/data/reference-session.pcap``（已在 .gitignore 里），
或用 ``EF_REFERENCE_PCAP`` 指到别处。**没有就跳过**，不是失败——解码链本身另有
``test_decoder.py`` 在无抓包环境下覆盖。

它还需要本机装着终末地客户端：``Bootstrap`` 要读 ``GameAssembly.dll`` 与
``global-metadata.dat`` 里那份静态 RSA 私钥，缺了解不开登录响应。
"""

from __future__ import annotations

import contextlib
import io
import os
import unittest
from argparse import Namespace
from pathlib import Path

from endfield_capture.selftest import run_selftest

DEFAULT_REFERENCE = Path(__file__).resolve().parent / "data" / "reference-session.pcap"


def reference_capture() -> Path | None:
    """参考抓包在哪；没有就返回 ``None``（调用方跳过）。"""

    override = os.environ.get("EF_REFERENCE_PCAP")
    path = Path(override) if override else DEFAULT_REFERENCE
    return path if path.is_file() else None


class ReferenceSessionTest(unittest.TestCase):
    def test_reference_capture_passes_selftest(self):
        """回放参考会话：数量、最后一条坐标、TCP 重组全部要对得上。"""

        pcap = reference_capture()
        if pcap is None:
            self.skipTest(
                "没有参考抓包。放一份到 " + str(DEFAULT_REFERENCE) + "，或用 EF_REFERENCE_PCAP 指定。"
                "它是一次完整会话（须含登录握手），不进仓库的理由见模块 docstring。"
            )

        args = Namespace(pcap=str(pcap), expect=None, game_dir=None)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = run_selftest(args)
        except SystemExit as exc:
            # 找不到客户端目录——这台机器跑不了自检，不是解码链的问题
            self.skipTest(f"没有可用的终末地客户端：{exc}")
        self.assertEqual(code, 0, "参考会话自检未通过")
