"""M4 交付 B.5：E7 goal.md 直写目标线白名单（默认 deny-all）。

覆盖 spec 交付 E.2 的写权限纪律：非白名单 folder_id -> 拒绝 + 留痕（reasons），
绝不静默放行；空白名单不授予任何写权限。纯函数测试，不碰任何 work-folder 写入。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet_graph.supervise.e7_allowlist import (
    E7WriteAllowlist,
    E7WriteAllowlistError,
    load_e7_write_allowlist,
    parse_e7_write_allowlist,
)


def allowlist(*folder_ids: str) -> E7WriteAllowlist:
    return parse_e7_write_allowlist({"folder_ids": list(folder_ids)})


class TestDefaultDenyAll:
    def test_empty_allowlist_grants_nothing(self) -> None:
        empty = E7WriteAllowlist.default()
        auth = empty.authorize("wf-a")
        assert auth.granted is False
        assert any("默认 deny-all" in r for r in auth.reasons)

    def test_default_factory_is_deny_all(self) -> None:
        assert E7WriteAllowlist().folder_ids == ()


class TestAuthorize:
    def test_folder_in_allowlist_grants(self) -> None:
        auth = allowlist("wf-a", "wf-b").authorize("wf-a")
        assert auth.granted is True

    def test_folder_outside_allowlist_is_refused_with_trace(self) -> None:
        auth = allowlist("wf-a").authorize("wf-other")
        assert auth.granted is False
        assert any("不在 E7 直写目标线白名单" in r for r in auth.reasons)

    def test_empty_folder_is_refused(self) -> None:
        auth = allowlist("wf-a").authorize("")
        assert auth.granted is False
        assert any("为空" in r for r in auth.reasons)

    def test_authorization_is_a_machine_readable_trace(self) -> None:
        as_dict = allowlist("wf-a").authorize("wf-x").as_dict()
        assert as_dict["granted"] is False
        assert isinstance(as_dict["reasons"], list)
        assert any("不在 E7 直写目标线白名单" in r for r in as_dict["reasons"])


class TestConfigParsing:
    def test_non_object_top_level_is_refused(self) -> None:
        with pytest.raises(E7WriteAllowlistError, match="JSON 对象"):
            parse_e7_write_allowlist(["wf-a"])

    def test_folder_ids_must_be_a_list(self) -> None:
        with pytest.raises(E7WriteAllowlistError, match="列表"):
            parse_e7_write_allowlist({"folder_ids": "wf-a"})

    def test_non_string_folder_id_is_refused(self) -> None:
        with pytest.raises(E7WriteAllowlistError, match="非空字符串"):
            parse_e7_write_allowlist({"folder_ids": ["wf-a", 3]})

    def test_non_wf_folder_id_is_refused(self) -> None:
        with pytest.raises(E7WriteAllowlistError, match="wf-"):
            parse_e7_write_allowlist({"folder_ids": ["dev-x"]})

    def test_missing_file_loads_deny_all(self, tmp_path: Path) -> None:
        loaded = load_e7_write_allowlist(tmp_path / "absent.json")
        assert loaded == E7WriteAllowlist.default()
        assert loaded.authorize("wf-a").granted is False

    def test_invalid_json_loads_deny_all(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        assert load_e7_write_allowlist(path) == E7WriteAllowlist.default()

    def test_valid_file_loads_and_grants(self, tmp_path: Path) -> None:
        path = tmp_path / "e7-allowlist.json"
        path.write_text(json.dumps({"folder_ids": ["wf-a"]}), encoding="utf-8")
        loaded = load_e7_write_allowlist(path)
        assert loaded.authorize("wf-a").granted is True
        assert loaded.authorize("wf-b").granted is False
