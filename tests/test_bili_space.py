"""bili_space 测试：分页、部分结果、降级三条路径全部 mock 掉网络。

B 站风控是 IP 级的、还会冷却，线上联调既慢又不可复现；这里把
`_space_page` / `_yt_dlp_page` 换成可控假货，把"被拦时绝不丢数据"这条
最重要的行为钉住。
"""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

from backend import bili_space, main
from backend.bili_space import UpVideo, fetch_up_videos


def _page_data(bvids: list[str], *, total: int, page_number: int, ps: int = 30) -> dict:
    """构造 space 接口 data 段，形状与线上一致。"""
    return {
        "page": {"count": total, "pn": page_number, "ps": ps},
        "list": {
            "vlist": [
                {
                    "bvid": bvid,
                    "title": f"标题-{bvid}",
                    "created": 1700000000 + index,
                    "length": "10:00",
                    "author": "UP主",
                }
                for index, bvid in enumerate(bvids)
            ]
        },
    }


def _bvids(start: int, count: int) -> list[str]:
    return [f"BV{index:010d}" for index in range(start, start + count)]


def test_recent_mode_returns_exactly_limit_and_stops(monkeypatch) -> None:
    """只要最近 N 条：取够就停，不再翻页，也不该报"还有后续"。"""
    requested_pages: list[int] = []

    def fake_page(uid, page_number, *, cookie, fingerprint):
        requested_pages.append(page_number)
        return _page_data(_bvids(page_number * 30, 30), total=900, page_number=page_number)

    monkeypatch.setattr(bili_space, "_space_page", fake_page)

    result = fetch_up_videos(672328094, mode="recent", limit=5, page_delay=0)

    # 返回按投稿时间倒序的"最近 5 条"（假数据里 BV59 最新）
    assert [video.bvid for video in result.videos] == _bvids(55, 5)[::-1]
    assert requested_pages == [1], "取够 limit 后不该再请求第二页"
    assert result.complete is True
    assert result.next_page is None
    assert result.source == "space_api"


def test_all_mode_is_capped_and_reports_next_page(monkeypatch) -> None:
    """单次调用有页数上限，没拉完必须给出可续传的 next_page。"""
    requested: list[int] = []

    def fake_page(uid, page_number, *, cookie, fingerprint):
        requested.append(page_number)
        return _page_data(_bvids(page_number * 30, 30), total=3000, page_number=page_number)

    monkeypatch.setattr(bili_space, "_space_page", fake_page)

    result = fetch_up_videos(1, mode="all", max_pages=3, page_delay=0)

    assert requested == [1, 2, 3]
    assert len(result.videos) == 90
    assert result.total == 3000
    assert result.complete is False
    assert result.next_page == 4
    assert "start_page=4" in result.message


def test_partial_result_survives_a_midway_block(monkeypatch) -> None:
    """半路被风控：把已拿到的交出去，complete=False，绝不整批丢弃。"""
    def fake_page(uid, page_number, *, cookie, fingerprint):
        if page_number == 2:
            raise RuntimeError("code=-352 风控校验失败")
        return _page_data(_bvids(page_number * 30, 30), total=3000, page_number=page_number)

    monkeypatch.setattr(bili_space, "_space_page", fake_page)

    result = fetch_up_videos(1, mode="all", max_pages=10, max_retries=1, page_delay=0)

    assert len(result.videos) == 30
    assert result.complete is False
    assert result.next_page == 2
    assert result.videos[0].url == f"https://www.bilibili.com/video/{result.videos[0].bvid}"


def test_first_page_block_falls_back_to_ytdlp(monkeypatch) -> None:
    """首页就进不去：退给 yt-dlp，它至少能给出地址。"""
    def always_blocked(*_args, **_kwargs):
        raise RuntimeError("HTTP 412")

    seen: dict[str, object] = {}

    def fake_ytdlp(uid, *, limit=None):
        seen["limit"] = limit
        return [UpVideo(bvid="BV1fallback", url="https://www.bilibili.com/video/BV1fallback")]

    monkeypatch.setattr(bili_space, "_space_page", always_blocked)
    monkeypatch.setattr(bili_space, "_yt_dlp_page", fake_ytdlp)

    result = fetch_up_videos(1, mode="recent", limit=3, max_retries=1, page_delay=0)

    assert [video.bvid for video in result.videos] == ["BV1fallback"]
    assert result.source == "yt_dlp"
    assert result.complete is True
    assert seen["limit"] == 3
    assert "yt-dlp" in result.message


def test_both_routes_blocked_returns_actionable_message(monkeypatch) -> None:
    """两条路都被拦：要说清是 IP 限流、该怎么办，而不是抛异常。"""
    def always_blocked(*_args, **_kwargs):
        raise RuntimeError("code=-352 风控校验失败")

    def also_blocked(*_args, **_kwargs):
        raise RuntimeError("Request is blocked by server (412)")

    monkeypatch.setattr(bili_space, "_space_page", always_blocked)
    monkeypatch.setattr(bili_space, "_yt_dlp_page", also_blocked)

    result = fetch_up_videos(1, mode="all", max_retries=1, page_delay=0)

    assert result.videos == []
    assert result.complete is False
    assert "412" in result.message or "352" in result.message
    assert "限流" in result.message


def test_end_of_list_is_complete(monkeypatch) -> None:
    """最后一页不满一页 = 后面没有了。"""
    def fake_page(uid, page_number, *, cookie, fingerprint):
        if page_number == 2:
            return _page_data(_bvids(200, 7), total=37, page_number=page_number)
        return _page_data(_bvids((page_number - 1) * 30, 30), total=37, page_number=page_number)

    monkeypatch.setattr(bili_space, "_space_page", fake_page)

    result = fetch_up_videos(1, mode="all", max_pages=10, page_delay=0)

    assert result.complete is True
    assert result.next_page is None
    assert len(result.videos) == 37


def test_fingerprint_is_reused_across_pages(monkeypatch) -> None:
    """同一轮拉取必须复用同一套 dm 指纹：每页都换屏幕参数本身就是机器行为。"""
    fingerprints: list[str] = []

    def fake_page(uid, page_number, *, cookie, fingerprint):
        fingerprints.append(fingerprint["dm_img_str"])
        return _page_data(_bvids(page_number * 30, 30), total=3000, page_number=page_number)

    monkeypatch.setattr(bili_space, "_space_page", fake_page)

    fetch_up_videos(1, mode="all", max_pages=4, page_delay=0)

    assert len(set(fingerprints)) == 1, f"四页用了四套指纹：{fingerprints}"


def test_risk_markers_are_recognised() -> None:
    for text in ("HTTP 412", "code=-352 风控校验失败", "code=-403 访问权限不足", "banned by RISK"):
        assert bili_space.looks_like_risk_control(text), text
    assert not bili_space.looks_like_risk_control("code=-400 请求错误")


def test_parse_space_page_keeps_timeline_fields(monkeypatch) -> None:
    """标题/投稿时间/时长必须原样带出来，它们是列表页唯一能认出视频的线索。"""
    data = _page_data(["BV1abcdefgh"], total=1, page_number=1)

    videos, total = bili_space._parse_space_page(data, 1)

    assert total == 1
    assert videos[0].title == "标题-BV1abcdefgh"
    assert videos[0].duration == "10:00"
    assert videos[0].author == "UP主"
    assert videos[0].created == 1700000000
    # 只校验格式不校验具体日期：upload_date 按本机时区格式化，断言日期会绑死时区
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", videos[0].as_dict()["upload_date"])


# ---- 本地增量存储：两个选项共用，绕开 IP 限流的关键 ----


def test_all_mode_writes_cache_for_later_calls(monkeypatch, tmp_path) -> None:
    def fake_page(uid, page_number, *, cookie, fingerprint):
        # total=37：第 1 页满 30 条，第 2 页只剩 7 条（不满一页 = 到底）
        count = 30 if page_number == 1 else 7
        return _page_data(
            _bvids((page_number - 1) * 30, count), total=37, page_number=page_number
        )

    monkeypatch.setattr(bili_space, "_space_page", fake_page)

    fetch_up_videos(1, mode="all", max_pages=5, page_delay=0, cache_dir=tmp_path)

    cached = bili_space.load_cache(tmp_path, 1)
    assert cached is not None
    assert len(cached["videos"]) == 37
    assert cached["complete"] is True
    # 缓存按投稿时间倒序，新的在前——增量就靠这个"撞到旧的即停"
    created = [item["created"] for item in cached["videos"]]
    assert created == sorted(created, reverse=True)


def test_all_mode_returns_local_view_with_only_new_added(monkeypatch, tmp_path) -> None:
    """本地已有 2 条、第 1 页是 1 新 + 2 旧：返回本地全部 3 条，只算新增 1 条，且只翻 1 页。"""
    bili_space.save_cache(
        tmp_path, 1,
        [
            UpVideo(bvid="BV1old00000000", url="https://www.bilibili.com/video/BV1old00000000",
                    title="旧1", created=1700000000),
            UpVideo(bvid="BV1old000000001", url="https://www.bilibili.com/video/BV1old000000001",
                    title="旧2", created=1690000000),
        ],
        complete=True, fetched_pages=1, total=3,
    )
    requested: list[int] = []

    def fake_page(uid, page_number, *, cookie, fingerprint):
        requested.append(page_number)
        # 倒序：新发布的排最前，随后是本地已知的两条
        return {
            "page": {"count": 3, "pn": page_number, "ps": 30},
            "list": {"vlist": [
                {"bvid": "BV1new000000000", "title": "新", "created": 1800000000,
                 "length": "1:00", "author": "UP"},
                {"bvid": "BV1old00000000", "title": "旧1", "created": 1700000000,
                 "length": "1:00", "author": "UP"},
                {"bvid": "BV1old000000001", "title": "旧2", "created": 1690000000,
                 "length": "1:00", "author": "UP"},
            ]},
        }

    monkeypatch.setattr(bili_space, "_space_page", fake_page)

    result = fetch_up_videos(1, mode="all", max_pages=10, page_delay=0, cache_dir=tmp_path)

    assert requested == [1], "撞到已知视频就该停，不该翻第二页"
    assert result.added == 1
    assert [video.bvid for video in result.videos] == [
        "BV1new000000000", "BV1old00000000", "BV1old000000001"
    ]
    assert result.cached_count == 3
    assert "新增 1 条" in result.message


def test_recent_mode_stores_whole_page_not_just_limit(monkeypatch, tmp_path) -> None:
    """最近 5 条：返回 5 条，但整页 30 条都要进缓存——只存 5 条会让"全部视频"缺一大块。"""
    requested: list[int] = []

    def fake_page(uid, page_number, *, cookie, fingerprint):
        requested.append(page_number)
        return _page_data(_bvids(0, 30), total=100, page_number=page_number)

    monkeypatch.setattr(bili_space, "_space_page", fake_page)

    result = fetch_up_videos(1, mode="recent", limit=5, page_delay=0, cache_dir=tmp_path)

    assert len(result.videos) == 5
    assert requested == [1]
    assert result.complete is True
    assert result.cached_count == 30
    assert len(bili_space.load_cache(tmp_path, 1)["videos"]) == 30


def test_recent_mode_reuses_local_cache_for_latest_n(monkeypatch, tmp_path) -> None:
    """本地已完整时选"最近 N 个"：也只查一次新增，然后直接给本地最新 5 条。"""
    bili_space.save_cache(
        tmp_path, 1,
        [UpVideo(bvid=bvid, url=f"https://www.bilibili.com/video/{bvid}", created=1700000000 + i)
         for i, bvid in enumerate(_bvids(0, 30))],
        complete=True, fetched_pages=1, total=30,
    )
    requested: list[int] = []

    def fake_page(uid, page_number, *, cookie, fingerprint):
        requested.append(page_number)
        # 全是已知视频：第 1 页就该停下
        return _page_data(_bvids(0, 30), total=30, page_number=page_number)

    monkeypatch.setattr(bili_space, "_space_page", fake_page)

    result = fetch_up_videos(1, mode="recent", limit=5, page_delay=0, cache_dir=tmp_path)

    assert len(result.videos) == 5
    assert result.added == 0
    assert requested == [1], "查新增只需一页"
    assert "没有新视频" in result.message


def test_recent_mode_skips_network_when_local_is_exhausted(monkeypatch, tmp_path) -> None:
    """本地完整但全部视频不足 N 条：无处可拉，不该发任何请求。"""
    def explode(*_args, **_kwargs):
        raise AssertionError("本地已到底时不该联网")

    monkeypatch.setattr(bili_space, "_space_page", explode)
    bili_space.save_cache(
        tmp_path, 1,
        [UpVideo(bvid=bvid, url=f"https://www.bilibili.com/video/{bvid}")
         for bvid in _bvids(0, 3)],
        complete=True, fetched_pages=1, total=3,
    )

    result = fetch_up_videos(1, mode="recent", limit=5, page_delay=0, cache_dir=tmp_path)

    assert len(result.videos) == 3
    assert result.source == "cache"
    assert result.complete is True
    assert "无需联网" in result.message


def test_all_mode_resumes_an_incomplete_cache(monkeypatch, tmp_path) -> None:
    """上次只拉到第 1 页：应该先补齐缺口，而不是从第 1 页重来。"""
    bili_space.save_cache(
        tmp_path, 1,
        [UpVideo(bvid=bvid, url=f"https://www.bilibili.com/video/{bvid}")
         for bvid in _bvids(0, 30)],
        complete=False, fetched_pages=1, total=90,
    )
    requested: list[int] = []

    def fake_page(uid, page_number, *, cookie, fingerprint):
        requested.append(page_number)
        return _page_data(_bvids((page_number - 1) * 30, 30), total=90, page_number=page_number)

    monkeypatch.setattr(bili_space, "_space_page", fake_page)

    result = fetch_up_videos(1, mode="all", max_pages=5, page_delay=0, cache_dir=tmp_path)

    assert requested[0] == 2, "应该从上次断的地方续，而不是从第 1 页重来"
    assert len(result.videos) == 90
    assert result.added == 60
    assert result.cached_count == 90
    assert bili_space.load_cache(tmp_path, 1)["complete"] is True


def test_failed_first_fetch_writes_no_empty_cache(monkeypatch, tmp_path) -> None:
    """全被拦时不能落一个空缓存：那会把「没有缓存」变成「缓存 0 条、不完整」。"""
    def always_blocked(*_args, **_kwargs):
        raise RuntimeError("code=-352 风控校验失败")

    monkeypatch.setattr(bili_space, "_space_page", always_blocked)
    monkeypatch.setattr(
        bili_space, "_yt_dlp_page",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("412")),
    )

    result = fetch_up_videos(5, mode="all", max_retries=1, page_delay=0, cache_dir=tmp_path)

    assert result.videos == []
    assert bili_space.load_cache(tmp_path, 5) is None, "失败不该留下空缓存"
    assert result.cached_count is None


def test_corrupt_cache_is_ignored_rather_than_fatal(monkeypatch, tmp_path) -> None:
    """缓存文件损坏时当作没有缓存，照常拉取并重建，不能让整个调用失败。"""
    path = tmp_path / bili_space.CACHE_DIR_NAME
    path.mkdir(parents=True)
    (path / "7.json").write_text("{ not json", encoding="utf-8")
    assert bili_space.load_cache(tmp_path, 7) is None

    monkeypatch.setattr(
        bili_space, "_space_page",
        lambda uid, page_number, *, cookie, fingerprint: _page_data(
            _bvids(0, 3), total=3, page_number=page_number
        ),
    )

    result = fetch_up_videos(7, mode="all", max_pages=2, page_delay=0, cache_dir=tmp_path)

    assert len(result.videos) == 3
    rebuilt = bili_space.load_cache(tmp_path, 7)
    assert rebuilt is not None and len(rebuilt["videos"]) == 3


# ---- API 层：参数校验与响应形状 ----


def test_api_rejects_bad_uid() -> None:
    client = TestClient(main.app)

    response = client.post("/api/bili-space-videos", json={"uid": 0})

    assert response.status_code == 400
    assert "UID" in response.json()["detail"]


def test_api_recent_mode_requires_limit() -> None:
    client = TestClient(main.app)

    response = client.post("/api/bili-space-videos", json={"uid": 1, "mode": "recent"})

    assert response.status_code == 400
    assert "limit" in response.json()["detail"]


def test_api_rejects_out_of_range_max_pages() -> None:
    client = TestClient(main.app)

    response = client.post(
        "/api/bili-space-videos", json={"uid": 1, "mode": "all", "max_pages": 999}
    )

    assert response.status_code == 400


def test_api_returns_partial_payload_shape(monkeypatch) -> None:
    """前端和 MCP 依赖这几个字段判断"还能不能继续"，形状不能被改没。"""
    def fake_fetch(uid, *, mode, limit, start_page, max_pages, cookie, cache_dir=None):
        assert cookie is None or isinstance(cookie, dict)
        assert cache_dir is not None, "API 必须把缓存目录传进来，否则增量更新无从谈起"
        return bili_space.UpVideoPage(
            uid=uid,
            videos=[UpVideo(bvid="BV1shape", url="https://www.bilibili.com/video/BV1shape")],
            total=100,
            page=start_page,
            fetched_pages=1,
            next_page=2,
            complete=False,
            source="space_api",
            message="还有后续",
        )

    monkeypatch.setattr(bili_space, "fetch_up_videos", fake_fetch)
    client = TestClient(main.app)

    payload = client.post(
        "/api/bili-space-videos", json={"uid": 42, "mode": "all", "start_page": 1}
    ).json()

    for key in ("uid", "videos", "count", "total", "next_page", "complete", "source", "message"):
        assert key in payload, f"缺少字段 {key}"
    assert payload["count"] == 1
    assert payload["videos"][0]["url"].startswith("https://www.bilibili.com/video/")
