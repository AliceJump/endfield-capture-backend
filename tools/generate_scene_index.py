"""从客户端解包产物生成运行时读的那张场景表（``data/scene_index.json``）。

**这是一次性的离线步骤**：运行时（``endfield_capture.scene_index``）只读 JSON，不生成
任何东西。客户端更新、或换了一份解包产物之后重跑本脚本即可。

数据源：

* ``LevelBasicInfoTable.json`` —— ``id``（关卡 id）与 ``idNum``（= 报文里的 sceneNumId），
  外加参考字段 ``domainName``；
* ``SpecialLevelToMapTable.json`` —— 少数关卡显式声明所属地图，随表一起核对；
* ``LevelDescTable.json`` + ``I18nTextTable_CN.json`` —— 关卡显示名；
* ``DungeonTable.json`` + ``DungeonSeriesTable.json`` + ``TextTable.json`` —— 场景号对应的
  副本、副本名与所属系列（按 ``DungeonTable.sceneId`` 反查到关卡上，不是 ``levelId``——
  那是副本加载用的底图，靶场那批全都写着 ``map02_lv009``）。

默认路径是本机那份解包产物的位置，换机器用对应参数覆盖：

    python tools/generate_scene_index.py                 # 写 src/endfield_capture/data/scene_index.json
    python tools/generate_scene_index.py --dry-run       # 只打印统计
    python tools/generate_scene_index.py --out <路径>
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_OUT = REPO_ROOT / "src" / "endfield_capture" / "data" / "scene_index.json"
DEFAULT_LEVELS = Path("E:/LuQing/end filed/Endfield_Map_Export/Json/GameplayConfig/LevelBasicInfoTable.json")
DEFAULT_SPECIAL = Path("E:/LuQing/end filed/Endfield_Map_Export/Readable/TableCfg_json/SpecialLevelToMapTable.json")
DEFAULT_DESC = Path("E:/LuQing/end filed/Endfield_Map_Export/Readable/TableCfg_json/LevelDescTable.json")
DEFAULT_I18N = Path("E:/LuQing/end filed/.tools/EndfieldUnpacker/DecryptOutput/TableCfg_json/I18nTextTable_CN.json")
DEFAULT_TEXT = Path("E:/LuQing/end filed/.tools/EndfieldUnpacker/DecryptOutput/TableCfg_json/TextTable.json")
DEFAULT_DUNGEONS = Path("E:/LuQing/end filed/.tools/EndfieldUnpacker/DecryptOutput/TableCfg_json/DungeonTable.json")
DEFAULT_SERIES = Path("E:/LuQing/end filed/.tools/EndfieldUnpacker/DecryptOutput/TableCfg_json/DungeonSeriesTable.json")

#: 大地图的显示名（取自客户端文本表：map01 / map02 来自 MapIdTable.showName，
#: base01 用 LevelDescTable 的关卡名而不是地图服务那边的「塔卫二」——那是行星名）
MAP_DISPLAY_NAMES: dict[str, str] = {
    "base01": "帝江号",
    "map01": "四号谷地",
    "map02": "武陵",
}

#: 显式声明所属地图的关卡（SpecialLevelToMapTable），优先级最高
SPECIAL_LEVEL_TO_MAP: dict[str, str] = {
    "dung02_dg005": "map02",
    "indie_dg005": "map02",
    "indie_dg006": "map02",
    "indie_dg007": "map02",
    "indie_dg008": "map02",
    "indie_dg016": "map02",
}


def load(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def parent_map(level_id: str) -> str:
    """关卡 id → 所属大地图 id；判定不出时返回空串。

    优先级：``SpecialLevelToMapTable`` 的显式声明 → 关卡 id 自带前缀 → 地下城前缀。
    ``domainName`` **不参与判定**：实测同一个前缀会横跨 ``domain_1`` / ``domain_2``
    （例如 ``dung02_cdg001`` 是 domain_1、``dung02_gdg002`` 是 domain_2）。
    """

    if level_id in SPECIAL_LEVEL_TO_MAP:
        return SPECIAL_LEVEL_TO_MAP[level_id]
    for prefix in ("base01", "map01", "map02"):
        if level_id == prefix or level_id.startswith(prefix + "_"):
            return prefix
    if level_id.startswith("dung01_"):
        return "map01"
    if level_id.startswith("dung02_"):
        return "map02"
    return ""


def load_rows(levels_path: Path) -> list[tuple[int, str, str]]:
    """读关卡表并排序；idNum 重复直接报错，不静默取其一。"""

    table = load(levels_path)
    rows = [(int(entry["idNum"]), level_id, entry.get("domainName", "")) for level_id, entry in table.items()]
    rows.sort(key=lambda row: (row[0], row[1]))
    seen: set[int] = set()
    for scene, level_id, _ in rows:
        if scene in seen:
            raise SystemExit(f"idNum 重复：{scene}（{level_id}）——解包产物有问题，先查清楚")
        seen.add(scene)
    return rows


def load_names(desc_path: Path, i18n: dict[str, str]) -> dict[str, str]:
    """``level_id`` → 关卡显示名（``LevelDescTable.showName`` 经文本表解出）。"""

    table = load(desc_path)
    names: dict[str, str] = {}
    for level_id, entry in table.items():
        text = i18n.get(str(entry.get("showName", {}).get("id")))
        if text:
            names[level_id] = text
    return names


def dungeon_name(entry: dict, series: dict, text: dict, i18n: dict[str, str]) -> str:
    """副本名的解析优先级，与导出侧 ``resolve_scene_tables.py`` 保持一致。"""

    dungeon_id = entry.get("dungeonId", "")
    text_value = i18n.get(str(entry.get("dungeonName", {}).get("id")))
    if text_value:
        return text_value
    key = text.get(dungeon_id)
    if key and i18n.get(str(key["id"])):
        return i18n[str(key["id"])]
    series_name = i18n.get(str(series.get(entry.get("dungeonSeriesId", ""), {}).get("name", {}).get("id")))
    if series_name:
        return series_name
    key = text.get(dungeon_id + "_title")
    if key and i18n.get(str(key["id"])):
        return i18n[str(key["id"])]
    return ""


def load_dungeons(
    dungeons_path: Path,
    series_path: Path,
    text_path: Path,
    i18n: dict[str, str],
    level_ids: set[str],
) -> dict[str, tuple[str, str, str, str, int]]:
    """按 ``sceneId`` 把副本挂到关卡上：``level_id → (dungeonId, 副本名, 系列名, 类别, 共用数)``。"""

    dungeons = load(dungeons_path)
    series = load(series_path)
    text = load(text_path)

    grouped: dict[str, list[str]] = {}
    for dungeon_id, entry in dungeons.items():
        scene = entry.get("sceneId") or ""
        if scene in level_ids:
            grouped.setdefault(scene, []).append(dungeon_id)

    table: dict[str, tuple[str, str, str, str, int]] = {}
    for scene, ids in grouped.items():
        ids.sort()
        picked = dungeons[ids[0]]
        table[scene] = (
            ids[0],
            dungeon_name(picked, series, text, i18n),
            i18n.get(str(series.get(picked.get("dungeonSeriesId", ""), {}).get("name", {}).get("id"))) or "",
            picked.get("dungeonCategory", ""),
            len(ids),
        )
    return table


def build_scenes(
    rows: list[tuple[int, str, str]],
    names: dict[str, str],
    dungeons: dict[str, tuple[str, str, str, str, int]],
) -> list[dict]:
    """编译成运行时读的行；关卡名缺失时退回副本名（干员支线那批只有副本侧有名字）。"""

    scenes = []
    for scene, level_id, domain in rows:
        dungeon_id, dungeon_name_text, series_name, category, dungeon_count = dungeons.get(
            level_id, ("", "", "", "", 0)
        )
        map_id = parent_map(level_id)
        scenes.append(
            {
                "scene": scene,
                "levelId": level_id,
                "levelName": names.get(level_id) or dungeon_name_text,
                "dungeonId": dungeon_id,
                "dungeonName": dungeon_name_text,
                "seriesName": series_name,
                "category": category,
                "dungeonCount": dungeon_count,
                "mapId": map_id,
                "mapName": MAP_DISPLAY_NAMES.get(map_id, ""),
                "domain": domain,
            }
        )
    return scenes


def check_special(special_path: Path) -> None:
    """核对本脚本里手写的显式地图声明与解包表一致。"""

    declared = {level_id: entry["mapId"] for level_id, entry in load(special_path).items()}
    if declared != SPECIAL_LEVEL_TO_MAP:
        print("！SpecialLevelToMapTable 与本脚本的 SPECIAL_LEVEL_TO_MAP 不一致：")
        for level_id in sorted(set(declared) | set(SPECIAL_LEVEL_TO_MAP)):
            got, want = SPECIAL_LEVEL_TO_MAP.get(level_id), declared.get(level_id)
            if got != want:
                print(f"    {level_id}: 脚本里 {got!r}，解包表 {want!r}")
        print("  请手工更新本脚本里的 SPECIAL_LEVEL_TO_MAP。")
    else:
        print(f"SpecialLevelToMapTable 与本脚本一致（{len(declared)} 条）。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出 JSON（默认打到包内 data/）")
    parser.add_argument("--levels", type=Path, default=DEFAULT_LEVELS, help="LevelBasicInfoTable.json")
    parser.add_argument("--special", type=Path, default=DEFAULT_SPECIAL, help="SpecialLevelToMapTable.json")
    parser.add_argument("--desc", type=Path, default=DEFAULT_DESC, help="LevelDescTable.json")
    parser.add_argument("--i18n", type=Path, default=DEFAULT_I18N, help="I18nTextTable_CN.json")
    parser.add_argument("--text", type=Path, default=DEFAULT_TEXT, help="TextTable.json")
    parser.add_argument("--dungeons", type=Path, default=DEFAULT_DUNGEONS, help="DungeonTable.json")
    parser.add_argument("--series", type=Path, default=DEFAULT_SERIES, help="DungeonSeriesTable.json")
    parser.add_argument("--dry-run", action="store_true", help="只打印统计，不写文件")
    args = parser.parse_args(argv)

    if args.special.is_file():
        check_special(args.special)
    else:
        print(f"！跳过 SpecialLevelToMapTable 核对：找不到 {args.special}")

    rows = load_rows(args.levels)
    i18n = load(args.i18n) if args.i18n.is_file() else {}
    names = load_names(args.desc, i18n) if (i18n and args.desc.is_file()) else {}
    dungeons = (
        load_dungeons(args.dungeons, args.series, args.text, i18n, {level_id for _, level_id, _ in rows})
        if (i18n and all(p.is_file() for p in (args.dungeons, args.series, args.text)))
        else {}
    )

    scenes = build_scenes(rows, names, dungeons)
    named = sum(1 for item in scenes if item["levelName"])
    bucket: dict[str, int] = {}
    for item in scenes:
        key = item["mapId"] or "(独立)"
        bucket[key] = bucket.get(key, 0) + 1
    print(f"场景 {len(scenes)} 条（有名字 {named}，挂到副本 {len(dungeons)}）：" + "，".join(f"{k} {v}" for k, v in sorted(bucket.items())))

    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": (
            "LevelBasicInfoTable.idNum + LevelDescTable + I18nTextTable_CN + "
            "DungeonTable + DungeonSeriesTable + TextTable + SpecialLevelToMapTable（客户端解包快照）"
        ),
        "maps": MAP_DISPLAY_NAMES,
        "scenes": scenes,
    }
    if args.dry_run:
        print("（--dry-run，不写文件）")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
