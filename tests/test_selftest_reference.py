"""参考会话回归：拿打进仓库的那份完整抓包跑一遍 ``selftest``。

``selftest.REFERENCE_EXPECTATION`` 里那组数值来自研究阶段的参考会话，``data/reference-session.pcap``
就是那份抓包本体。把它固定下来，自检才**可复现**——否则只能靠"手上还留着某次 runs/ 产物"，
而参考会话恰恰不是随时重抓得到的（要有一次从登录握手开始的完整会话）。

需要本机装着终末地客户端：``Bootstrap`` 要读 ``GameAssembly.dll`` 与 ``global-metadata.dat``
里那份静态 RSA 私钥，缺了没法解登录响应。**没有客户端就跳过而不是失败**——解码链本身另有
``test_decoder.py`` 在无客户端环境覆盖。
"""

from __future__ import annotations

import contextlib
import io
import unittest
from argparse import Namespace
from pathlib import Path

from endfield_capture.selftest import REFERENCE_EXPECTATION, run_selftest

REFERENCE_PCAP = Path(__file__).resolve().parent / "data" / "reference-session.pcap"


class ReferenceSessionTest(unittest.TestCase):
    def test_reference_capture_passes_selftest(self):
        """回放参考会话：数量、最后一条坐标、TCP 重组全部要对得上。"""

        if not REFERENCE_PCAP.is_file():
            self.skipTest(f"缺参考抓包 {REFERENCE_PCAP}")

        args = Namespace(pcap=str(REFERENCE_PCAP), expect=None, game_dir=None)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = run_selftest(args)
        except SystemExit as exc:
            # 找不到客户端目录——这台机器跑不了自检，不是解码链的问题
            self.skipTest(f"没有可用的终末地客户端：{exc}")
        self.assertEqual(code, 0, "参考会话自检未通过")

    def test_reference_expectation_matches_the_committed_capture(self):
        """这份抓包必须就是 ``REFERENCE_EXPECTATION`` 描述的那一次。

        与上一条不同，这条不需要客户端、也不解码：只确认那份抓包还在。
        数值对不上时上一条会失败，这条只负责在抓包被误换/误删时给出更直白的提示。
        """

        self.assertTrue(REFERENCE_PCAP.is_file(), f"缺 {REFERENCE_PCAP}；它是自检的参考会话")
        self.assertGreater(REFERENCE_PCAP.stat().st_size, 0)
        self.assertEqual(
            REFERENCE_EXPECTATION["sceneNumId"],
            232,
            "参考会话发生在场景 232（试验园区）",
        )
