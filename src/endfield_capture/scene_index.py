"""场景号 → 关卡 → 中文名 → 副本/系列 → 大地图 的查表。

运行时**只读预存的表** ``data/scene_index.json``：本模块不解包、不推导、不生成任何东西。
表由 ``tools/generate_scene_index.py`` 从客户端解包产物离线生成一次；换机器或用别的客户端
快照时，用环境变量 ``EF_SCENE_INDEX`` 指向另一份 JSON 即可。

**读不到表就拒绝启动**——那和「这个号查不到」是两回事，不能混成静默的空值。查不到的号返回
``None``，字段一律留空，不做任何推测。

``sceneNumId`` 就是客户端 ``LevelBasicInfoTable`` 的 ``idNum``（抓包核对过：102 / 228 / 232
分别落在 ``map02_lv007`` / ``map02_lv002`` / ``map02_lv005``）。表里只有这一个编号空间；
客户端另外两套编号——``MapConfig.mapId`` 与 ``LevelShortIdTable`` 的短 id——**会与它撞车**
（``2`` 既是 ``map01`` 的地图号、又是 ``map01_lv002`` 的场景号），查号时不要混用。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "DATA_PATH",
    "GENERATED_AT",
    "MAP_DISPLAY_NAMES",
    "SCENE_INDEX",
    "SceneIndexError",
    "SceneInfo",
    "load_scene_index",
    "resolve_scene",
    "scene_index_payload",
    "scene_index_rows",
]

#: 预存表的位置；用环境变量 ``EF_SCENE_INDEX`` 指到别处
DATA_PATH = Path(
    os.environ.get("EF_SCENE_INDEX") or (Path(__file__).with_name("data") / "scene_index.json")
)


class SceneIndexError(RuntimeError):
    """预存表读不到或不合法。"""


@dataclass(frozen=True)
class SceneInfo:
    """一个场景号的静态解析结果。"""

    scene: int
    level_id: str
    level_name: str
    dungeon_id: str
    dungeon_name: str
    series_name: str
    category: str
    dungeon_count: int
    map_id: str
    map_name: str
    domain: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene": self.scene,
            "levelId": self.level_id,
            "levelName": self.level_name,
            "dungeonId": self.dungeon_id,
            "dungeonName": self.dungeon_name,
            "seriesName": self.series_name,
            "category": self.category,
            "dungeonCount": self.dungeon_count,
            "mapId": self.map_id,
            "mapName": self.map_name,
            "domain": self.domain,
        }


def load_scene_index(
    path: Path | str | None = None,
) -> tuple[dict[str, str], dict[int, SceneInfo], str]:
    """读预存的场景表，返回 ``(地图显示名, 查表, 生成时间)``。

    任何读不了、结构不对的情况都抛 :class:`SceneIndexError`，不静默降级。
    """

    target = Path(path) if path is not None else DATA_PATH
    if not target.is_file():
        raise SceneIndexError(
            f"读不到场景表 {target}。用 tools/generate_scene_index.py 生成一份，"
            "或用环境变量 EF_SCENE_INDEX 指向别处的表。"
        )
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SceneIndexError(f"场景表 {target} 读不了：{exc}") from exc

    maps = payload.get("maps") or {}
    index: dict[int, SceneInfo] = {}
    for row in payload.get("scenes") or []:
        try:
            scene = int(row["scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SceneIndexError(f"场景表 {target} 里有一行取不到场景号：{row!r}") from exc
        if scene in index:
            raise SceneIndexError(f"场景表 {target} 里场景号 {scene} 重复")
        index[scene] = SceneInfo(
            scene=scene,
            level_id=row.get("levelId", ""),
            level_name=row.get("levelName", ""),
            dungeon_id=row.get("dungeonId", ""),
            dungeon_name=row.get("dungeonName", ""),
            series_name=row.get("seriesName", ""),
            category=row.get("category", ""),
            dungeon_count=int(row.get("dungeonCount") or 0),
            map_id=row.get("mapId", ""),
            map_name=row.get("mapName", ""),
            domain=row.get("domain", ""),
        )
    if not index:
        raise SceneIndexError(f"场景表 {target} 里一条场景都没有")
    return maps, index, str(payload.get("generated_at") or "")


#: 地图 id → 显示名（来自预存表）
MAP_DISPLAY_NAMES: dict[str, str]
#: ``sceneNumId`` → 关卡等；查不到就是查不到，见 :func:`resolve_scene`
SCENE_INDEX: dict[int, SceneInfo]
#: 预存表的生成时间（来自表本身）
GENERATED_AT: str
MAP_DISPLAY_NAMES, SCENE_INDEX, GENERATED_AT = load_scene_index()


def resolve_scene(scene_num_id: int | str | None) -> SceneInfo | None:
    """查一个场景号；表里没有就返回 ``None``，不猜。"""

    try:
        key = int(scene_num_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return SCENE_INDEX.get(key)


def scene_index_rows() -> list[dict[str, Any]]:
    """整张表，按场景号升序。"""

    return [info.to_dict() for _, info in sorted(SCENE_INDEX.items())]


def scene_index_payload() -> dict[str, Any]:
    """``/api/scene-index`` 的响应体。"""

    return {
        "count": len(SCENE_INDEX),
        "maps": MAP_DISPLAY_NAMES,
        "generatedAt": GENERATED_AT,
        "dataFile": str(DATA_PATH),
        "source": "预存表 data/scene_index.json（由 tools/generate_scene_index.py 从客户端解包产物生成）",
        "scenes": scene_index_rows(),
    }
