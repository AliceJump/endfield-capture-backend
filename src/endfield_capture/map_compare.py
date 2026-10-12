"""可选的官方地图 WS 对照。

抓包解出的坐标与游戏地图服务下发的坐标是**同一世界坐标系**（实测逐位一致，配对最大距离
2e-5），所以两路可以逐条互校。本模块把官方 WS 的位置也收下来，供看板显示与统计。

**这是可选的**，因为它复用 `ok-end-field` 仓库里已经做好的 WS 认证（hg grant → cred /
sign_token → websocket）。找不到那个仓库时程序照常工作，只是没有对照。

> 许可提示：`ok-end-field` 是 AGPL-3.0。一旦启用本模块，本项目就与它链接在一起，
> 分发时需按 AGPL-3.0 处理。
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

__all__ = ["build_comparison_class", "resolve_account_content", "start_comparison"]


def resolve_account_content(repo: Path, account: str = "", content: str = "") -> str:
    """确定用哪份地图 content（仓库把它叫 content，实际是 hg token）。

    仓库自己的 ``get_account_map_content`` 比"配置里恰好只有一份"可靠得多：后者在
    多账号机器上必然失败。
    """

    if content:
        return content.strip()
    if account:
        import sys

        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))
        from src.tasks.account.account_scope_store import get_account_map_content

        resolved = get_account_map_content(account)
        if not resolved:
            raise ValueError(f"账号 {account} 没有可用的地图 content")
        return resolved
    store = json.loads((repo / "configs" / "account_scoped_overrides.json").read_text(encoding="utf-8-sig"))
    values = [v.strip() for v in store.get("map_contents", {}).values() if isinstance(v, str) and v.strip()]
    if len(values) == 1:
        return values[0]
    usernames = [line.strip() for line in str(store.get("account_list_text") or "").splitlines() if line.strip()]
    choices = "、".join(usernames) or "、".join(store.get("map_contents", {}))
    raise ValueError(f"账号配置里有 {len(values)} 份地图 content，需要用 --account 指定（可选：{choices}）")


def build_comparison_class(repo: Path):
    """在 ok-end-field 可导入的前提下，构造对照类。"""

    import sys

    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from src.localization.ws_position_mixin import WsPositionMixin

    class MapComparison(WsPositionMixin):
        def __init__(self, state, stop, output, content: str):
            self.state = state
            self.stop = stop
            self.output = output
            self._content = content
            self._init_ws_position_mixin()

        def _map_ws_should_stop_for_game_window(self):
            # 本程序是被动旁观者，没有 executor，仓库默认实现会去查
            # self.executor.device_manager.hwnd_window.hwnd，恒为 None，于是刚连上就被
            # 看门狗掐掉。这里显式关掉该判据。
            return False

        def _map_ws_should_stop_for_idle_consumer(self):
            return self.stop.is_set()

        def safe(self, text):
            text = str(text)
            for secret in (self._content, self._map_ws_cred, self._map_ws_sign_token):
                if secret:
                    text = text.replace(secret, "[已隐藏]")
            return re.sub(r'(?i)(token|cred|content)(["\s:=>]+)[^\s,}]+', r"\1\2[已隐藏]", text)[:220]

        def log_info(self, text):
            message = self.safe(text)
            with self.state.lock:
                if "已连接:" in message:
                    self.state.ws["status"] = "已连接，等待位置"
                elif "已解析角色" in message:
                    self.state.ws["status"] = "已认证，等待位置"
            if "收到位置:" not in message:
                self.state.event(message)

        def log_error(self, text):
            message = self.safe(text)
            with self.state.lock:
                self.state.ws["status"] = "连接重试中"
                self.state.ws["error"] = message
            self.state.event(message)

        def _push_ws_payload(self, payload):
            received_at = time.time()
            super()._push_ws_payload(payload)
            position, map_id, x, y, z = self._extract_position_payload(payload)
            if position is None:
                return
            stamps = []
            for name, container in (("message", payload), ("data", payload.get("data") or {}), ("pos", position)):
                if not isinstance(container, dict):
                    continue
                for key in ("timestamp", "serverTime", "server_time", "time", "ts"):
                    raw = container.get(key)
                    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.isdigit()):
                        stamps.append({"field": f"{name}.{key}", "raw": raw})
            with self.state.lock:
                self.state.ws["samples"] += 1
                item = {
                    "sampleId": self.state.ws["samples"],
                    "receivedAt": received_at,
                    "mapId": map_id,
                    "x": x,
                    "y": y,
                    "z": z,
                    "serverTimestamps": stamps,
                }
                self.state.ws.update(status="正在接收位置", lastPosition=item, error=None)
                self.state.ws_points.append(item)
            with self.output.open("a", encoding="utf-8") as file:
                file.write(json.dumps(item, ensure_ascii=False) + "\n")

        def run(self):
            try:
                while not self.stop.is_set():
                    if not self._is_map_ws_client_enabled():
                        with self.state.lock:
                            self.state.ws["status"] = "正在认证并连接"
                        if not self._start_map_ws_client(self._content):
                            self.stop.wait(30)
                            continue
                    self.stop.wait(1)
            except Exception as exc:
                self.log_error(type(exc).__name__ + ": " + str(exc))
            finally:
                self._stop_map_ws_client()

    return MapComparison


def start_comparison(state, stop, output: Path, repo: Path, content: str):
    """启动对照线程；失败只记状态，不影响抓包解码。"""

    try:
        comparison = build_comparison_class(repo)(state, stop, output, content)
    except Exception as exc:
        with state.lock:
            state.ws.update(status="未启用", error=f"{type(exc).__name__}: {str(exc)[:120]}")
        return None
    thread = threading.Thread(target=comparison.run, name="map-ws", daemon=True)
    thread.start()
    return comparison
