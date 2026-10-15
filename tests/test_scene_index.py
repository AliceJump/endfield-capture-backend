"""场景表的契约测试。

运行时只读预存的 ``data/scene_index.json``，所以这里钉两件事：**表本身**（抓包里实测过的
映射、唯一性、名字覆盖率、越界场景不猜）和**读取器**（读不到 / 结构不对都必须报错，
不能静默降级成空表）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from endfield_capture.scene_index import (
    DATA_PATH,
    MAP_DISPLAY_NAMES,
    SCENE_INDEX,
    SceneIndexError,
    SceneInfo,
    load_scene_index,
    resolve_scene,
    scene_index_payload,
    scene_index_rows,
)


class ResolveSceneTest(unittest.TestCase):
    def test_captured_scenes_resolve_to_map02(self):
        """102 / 228 / 232 是抓包里实测出现过的三个场景号，坐标都落在 map02 主网格里。"""

        for scene, level_id in ((102, "map02_lv007"), (228, "map02_lv002"), (232, "map02_lv005")):
            info = resolve_scene(scene)
            self.assertIsInstance(info, SceneInfo, scene)
            self.assertEqual((info.level_id, info.map_id, info.map_name), (level_id, "map02", "武陵"))

    def test_captured_scene_carries_level_name(self):
        self.assertEqual(resolve_scene(232).level_name, "试验园区")
        self.assertEqual(resolve_scene(228).level_name, "武陵城")
        self.assertEqual(resolve_scene(102).level_name, "应龙关")

    def test_scene_bound_to_dungeon_carries_dungeon_and_series(self):
        """dung01_cdg011 既是关卡也是副本场景，四层信息都要能查出来。"""

        info = resolve_scene(310)
        self.assertEqual((info.level_id, info.level_name), ("dung01_cdg011", "物以类聚"))
        self.assertEqual(info.dungeon_id, "dung01_actmonster01")
        self.assertEqual(info.dungeon_name, "物以类聚")
        self.assertEqual(info.series_name, "生存特训")
        self.assertEqual(info.category, "dungeon_actmonster")
        self.assertEqual(info.dungeon_count, 1)

    def test_shared_scene_reports_dungeon_count(self):
        """一个场景被多个关卡共用时，副本数要如实写出来而不是只留一个。"""

        info = resolve_scene(403)
        self.assertEqual(info.level_id, "dung01_ttdg001")
        self.assertEqual(info.dungeon_count, 16)

    def test_level_name_falls_back_to_dungeon_name(self):
        """rcdg 那批没有 LevelDescTable 条目，名字只能从副本侧拿。"""

        info = resolve_scene(404)
        self.assertEqual((info.level_id, info.level_name), ("dung02_rcdg001", "登楼阁"))
        self.assertEqual(info.dungeon_name, "登楼阁")

    def test_nameless_scene_keeps_fields_empty(self):
        """关卡存在但客户端没给名字时不编，字段留空。"""

        info = resolve_scene(468)
        self.assertEqual(info.level_id, "dung02_gdg013")
        self.assertEqual((info.level_name, info.dungeon_id, info.dungeon_name), ("", "", ""))
        self.assertEqual(info.map_id, "map02", "名字缺了不影响地图归属")

    def test_level_id_number_is_the_scene_number(self):
        """表是按 idNum 编的，``scene`` 必须与查询键一致。"""

        for scene, info in SCENE_INDEX.items():
            self.assertEqual(info.scene, scene)

    def test_other_maps(self):
        self.assertEqual(resolve_scene(2).level_id, "map01_lv002")
        self.assertEqual(resolve_scene(2).map_id, "map01")
        self.assertEqual(resolve_scene(99).map_id, "base01")

    def test_special_level_to_map_overrides_prefix(self):
        """``indie_dg007``（首墩内部）不在 map02_ 前缀下，靠 SpecialLevelToMapTable 归到 map02。"""

        info = resolve_scene(262)
        self.assertEqual((info.level_id, info.map_id), ("indie_dg007", "map02"))
        self.assertEqual(resolve_scene(370).map_id, "map02", "dung02_dg005")

    def test_dungeon_prefix_falls_back_to_world(self):
        # dung02_rdg015 的 domainName 是 domain_1，按 domain 判会错；按前缀才是 map02。
        info = resolve_scene(575)
        self.assertEqual((info.level_id, info.map_id, info.domain), ("dung02_rdg015", "map02", "domain_1"))

    def test_standalone_levels_have_no_map(self):
        info = resolve_scene(89)  # blackbox_basic_1：自己就是 MapIdTable 里的一项
        self.assertEqual(info.map_id, "")
        self.assertEqual(info.map_name, "")

    def test_unknown_scene_is_not_guessed(self):
        """查不到就返回 None，绝不推测。

        这里曾经写死过 ``463``，理由写着「解包数据里没有这个场景号」。那个理由本身就是错的——
        463（遂明）是个**正常场景号**，只是当时的导出漏了它。所以现在不写死具体数字，改成
        从表里推一个**确定不在**的号：拿最大的场景号再往上跳一大段，表不长出上千条就不会撞上。
        """

        absent = max(SCENE_INDEX) + 1000
        for bad in (0, -1, absent, None, "not-a-number", ""):
            self.assertIsNone(resolve_scene(bad), bad)

    def test_accepts_numeric_strings(self):
        # sceneMaps 的键是字符串，查表要能吃下去
        self.assertEqual(resolve_scene("232").level_id, "map02_lv005")


class TableShapeTest(unittest.TestCase):
    def test_size_matches_exported_level_table(self):
        """关卡数与本机客户端的解包表一致（**221**）。

        这个数曾经是 216，而 216 不是真值——当时的导出只读了 ``StreamingAssets/VFS``，漏掉
        ``Persistent/VFS`` 那 5 个关卡（见 ``test_persistent_vfs_levels_are_present``）。
        所以这条同时是**导出完整性的体检**：数字掉回去，就说明导出又只读了一个 VFS 根。
        客户端更新后这个数会变，届时重新解包核对再改这里。
        """

        self.assertEqual(
            len(SCENE_INDEX), 221, "本机客户端两个 VFS 根合并后是 221 个关卡；掉回 216 说明导出漏了 Persistent"
        )

    def test_persistent_vfs_levels_are_present(self):
        """这 5 个关卡**只存在于 Persistent/VFS**，缺席就说明导出又漏了那个根。

        它们各自在 ``StreamingAssets/VFS`` 里的 ``LevelConfig`` 根本不存在，所以只读那个根
        的导出必然少这一批。比单纯断言总数更精确——总数对了也可能是一增一减。
        """

        expected = {
            463: "indie_dg016",  # 遂明
            525: "indie_hdg026",  # 兽影生杀
            526: "indie_hdg028",  # 沛然轰鸣
            527: "indie_hdg027",  # 暗曜白霆
            550: "indie_hdg029",  # 三角定位
        }
        for scene, level_id in expected.items():
            info = resolve_scene(scene)
            self.assertIsNotNone(info, f"场景 {scene}（{level_id}）缺席：导出很可能只读了 StreamingAssets/VFS")
            self.assertEqual(info.level_id, level_id)

    def test_level_ids_are_unique(self):
        ids = [info.level_id for info in SCENE_INDEX.values()]
        self.assertEqual(len(set(ids)), len(ids))

    def test_map_ids_are_known_and_named(self):
        for info in SCENE_INDEX.values():
            self.assertIn(info.map_id, {"", *MAP_DISPLAY_NAMES})
            if info.map_id:
                self.assertTrue(info.map_name, f"{info.level_id} 缺地图显示名")

    def test_map_names_match_client_text(self):
        """map01 / map02 取自客户端文本表；「四号谷底」那个写法客户端里没有。"""

        self.assertEqual(MAP_DISPLAY_NAMES["map01"], "四号谷地")
        self.assertEqual(MAP_DISPLAY_NAMES["map02"], "武陵")
        self.assertEqual(resolve_scene(2).map_name, "四号谷地")

    def test_stale_spelling_is_not_reintroduced(self):
        repo_root = Path(__file__).resolve().parents[1]
        targets = [repo_root / "README.md", *sorted((repo_root / "src").rglob("*.py"))]
        stale = [
            str(path.relative_to(repo_root))
            for path in targets
            if path.is_file() and "四号谷底" in path.read_text(encoding="utf-8", errors="ignore")
        ]
        self.assertEqual(stale, [], "客户端文本表里没有「四号谷底」，别把这个写法写回来")

    def test_rows_are_sorted_and_complete(self):
        rows = scene_index_rows()
        self.assertEqual([row["scene"] for row in rows], sorted(row["scene"] for row in rows))
        self.assertEqual(len(rows), len(SCENE_INDEX))
        for row in rows:
            self.assertEqual(
                set(row),
                {
                    "scene", "levelId", "levelName", "dungeonId", "dungeonName",
                    "seriesName", "category", "dungeonCount", "mapId", "mapName", "domain",
                },
            )

    def test_most_levels_have_a_name(self):
        """名字覆盖率。**192 是当前真值**——别为了让它过就回去动生成器。

        这个数曾经是 205：多出来的 13 个来自生成器里「关卡名缺失就退回副本系列名」那一级，
        而它兜出来的是**分类标签**「支线任务」，不是地名。那一级已经删掉（见
        ``tools/generate_scene_index.py`` 的 ``dungeon_name``），所以覆盖率下降是对的。
        下限留一点余量，是给客户端小版本更新时增删关卡用的。
        """

        named = sum(1 for info in SCENE_INDEX.values() if info.level_name)
        self.assertGreaterEqual(named, 190, "名字覆盖率掉了，多半是生成时漏了文本表")

    #: 这些是**分类标签**，不是地名，任何一列都不该出现。
    GENERIC_LABELS = ("支线任务",)

    def test_generic_labels_are_not_used_as_names(self):
        """守卫：分类标签不许再灌进地名列。

        生成器曾经在关卡名/副本名取不到时退回**副本系列名**，把「支线任务」写进了 21 个
        场景的 ``dungeonName``、其中 13 个的 ``levelName``——看起来像真有个地区叫这个名字，
        而消费方（ok-end-field）会把它当地名用。取不到就留空，这条钉住那个回退别再回来。
        """

        for scene, info in SCENE_INDEX.items():
            for field, value in (("levelName", info.level_name), ("dungeonName", info.dungeon_name)):
                self.assertNotIn(
                    value,
                    self.GENERIC_LABELS,
                    f"场景 {scene} 的 {field} 是分类标签 {value!r}，不是地名",
                )

    def test_payload_shape(self):
        payload = scene_index_payload()
        self.assertEqual(payload["count"], len(SCENE_INDEX))
        self.assertEqual(payload["maps"], MAP_DISPLAY_NAMES)
        self.assertEqual(len(payload["scenes"]), payload["count"])


class LoaderTest(unittest.TestCase):
    """运行时只读预存表：读不到、结构不对都要炸，不能静默变成空表。"""

    def test_bundled_table_is_present(self):
        self.assertTrue(DATA_PATH.is_file(), f"缺 {DATA_PATH}；跑 tools/generate_scene_index.py 生成")

    def test_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SceneIndexError) as caught:
            load_scene_index(Path(tmp) / "nope.json")
        self.assertIn("读不到场景表", str(caught.exception))

    def test_broken_json_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{ not json", encoding="utf-8")
            with self.assertRaises(SceneIndexError):
                load_scene_index(path)

    def test_duplicate_scene_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dup.json"
            row = {"scene": 1, "levelId": "map01_lv001"}
            path.write_text(json.dumps({"maps": {}, "scenes": [row, row]}), encoding="utf-8")
            with self.assertRaises(SceneIndexError) as caught:
                load_scene_index(path)
        self.assertIn("重复", str(caught.exception))

    def test_empty_table_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.json"
            path.write_text(json.dumps({"maps": {}, "scenes": []}), encoding="utf-8")
            with self.assertRaises(SceneIndexError):
                load_scene_index(path)

    def test_round_trip_from_the_packaged_file(self):
        maps, index, generated_at = load_scene_index(DATA_PATH)
        self.assertEqual(maps, MAP_DISPLAY_NAMES)
        self.assertEqual(len(index), len(SCENE_INDEX))
        self.assertTrue(generated_at, "表里应该带生成时间")


if __name__ == "__main__":
    unittest.main()
