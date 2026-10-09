"""space_page 测试：Playwright 抓取路线的解析、重试与翻页收集。

真实浏览器与真实页面都不进单测（开窗口、依赖网络和 B 站状态），这里把
`_open / _goto / _js / _close` 四个钩子换成假实现，钉住四件事：

1. 页面偶发没渲染出列表时**会重开页面重试**，而不是直接报"没视频"；
2. 页面顶部写着总数却一条都没渲染出来 → 说的是"数据请求被拦"，不是"UP 主没投稿"；
3. **逐页点分页器**，每一页的卡片（和接口响应）都要累计下来；
4. 接口响应与 DOM 卡片两条来源合并，接口的精确时间戳优先。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from backend import main, space_page
from backend.space_page import crawl_space_page, parse_relative_time, parse_space_input


@dataclass
class FakeBrowser:
    probes: list[dict] = field(default_factory=list)
    collects: list[list[dict]] = field(default_factory=list)
    # 每次点"下一页"返回什么：False = 已到最后一页
    next_results: list[bool] = field(default_factory=lambda: [True])
    api_pages: list[list[dict]] = field(default_factory=list)
    navigations: list[str] = field(default_factory=list)
    next_clicks: int = 0
    clicked_pages: list[str] = field(default_factory=list)


def install(monkeypatch, browser: FakeBrowser) -> None:
    """把浏览器钩子换成假实现；脚本按对象身份区分。"""
    probe_index = {"i": 0}
    collect_index = {"i": 0}
    api_index = {"i": 0}
    session = object()

    def fake_js(_session, script):
        if script is space_page._PROBE_SCRIPT:
            if not browser.probes:
                return {"rendered": 0, "total": 0}
            index = min(probe_index["i"], len(browser.probes) - 1)
            probe_index["i"] += 1
            return browser.probes[index]
        if script is space_page._COLLECT_SCRIPT:
            if not browser.collects:
                return []
            index = min(collect_index["i"], len(browser.collects) - 1)
            collect_index["i"] += 1
            return browser.collects[index]
        if "// click-page" in script:
            browser.next_clicks += 1
            # 记录点了哪一页，方便断言"缺页要能被定位到"
            marker = "String("
            if marker in script:
                browser.clicked_pages.append(script.split(marker, 1)[1].split(")", 1)[0])
            if not browser.next_results:
                return False
            return browser.next_results.pop(0)
        if script is space_page._ACTIVE_PAGE_SCRIPT:
            return 1
        raise AssertionError(f"不认识的页面脚本：{str(script)[:60]}")

    monkeypatch.setattr(space_page, "_open", lambda: session)
    monkeypatch.setattr(space_page, "_goto", lambda _s, url: browser.navigations.append(url))
    monkeypatch.setattr(space_page, "_js", fake_js)
    monkeypatch.setattr(space_page, "_close", lambda _s: None)
    monkeypatch.setattr(
        space_page,
        "_api_items",
        lambda _s: browser.api_pages[min(api_index["i"], len(browser.api_pages) - 1)]
        if browser.api_pages else [],
    )


def _bvid(seed: str) -> str:
    """造合法且互不相同的 BV（BV + 10 位十六进制）。

    两个坑都踩过：
    - 长度不对的 BV 会被 `_video_from_dom` 的 BV_RE **静默丢掉**，测试拿到 0 条，
      失败原因却显示成"没抓到视频"；
    - 用 `seed.ljust(10, '0')` 补位会碰撞（seed10 与 seed100 都变成 seed100000），
      缓存里的条数比预期少，增量判断随之错位。
    """
    import hashlib

    return f"BV{hashlib.md5(seed.encode()).hexdigest()[:10]}"


def _card(bvid: str, title: str = "", published: str = "") -> dict:
    return {
        "bvid": bvid,
        "url": f"https://www.bilibili.com/video/{bvid}",
        "title": title,
        "published": published,
        "duration": "03:03",
    }


def _api_item(bvid: str, title: str = "", created: int = 0) -> dict:
    return {"bvid": bvid, "title": title, "created": created, "duration": "05:00", "author": "UP"}


NOSLEEP = lambda _seconds: None  # noqa: E731


def test_parse_space_input_accepts_full_url_and_bare_uid() -> None:
    url, uid = parse_space_input("https://space.bilibili.com/3546865492560315/upload/video")
    assert uid == "3546865492560315"
    assert url.endswith("/upload/video")

    url, uid = parse_space_input("  space.bilibili.com/672328094  ")
    assert uid == "672328094"
    assert "672328094" in url

    url, uid = parse_space_input("123456")
    assert (uid, url) == ("123456", "https://space.bilibili.com/123456/upload/video")


def test_parse_space_input_rejects_garbage() -> None:
    for bad in ("", "   ", "https://www.bilibili.com/video/BV1xx", "space.bilibili.com/upload/video"):
        with pytest.raises(ValueError):
            parse_space_input(bad)


@pytest.mark.parametrize(
    ("text", "expected_seconds_ago"),
    [
        ("4小时前", 4 * 3600),
        ("2天前", 2 * 86400),
        ("30分钟前", 30 * 60),
        ("1年前", 365 * 86400),
    ],
)
def test_parse_relative_time(text: str, expected_seconds_ago: int) -> None:
    now = 1_800_000_000
    assert parse_relative_time(text, now=now) == now - expected_seconds_ago


def test_parse_relative_time_handles_absolute_and_unparseable() -> None:
    assert parse_relative_time("2025-01-05") > 0
    assert parse_relative_time("置顶") == 0, "解析不出来要返回 0（排最后），不能抛异常"
    assert parse_relative_time("") == 0


def test_parse_relative_time_handles_chinese_date_without_year() -> None:
    """当年内的老视频只写 `8月27日`，不认它就等于这批视频全都没有时间。"""
    import datetime as _datetime

    now = int(_datetime.datetime(2026, 10, 3, 12, 0).timestamp())

    parsed = parse_relative_time("8月27日", now=now)
    assert parsed == int(_datetime.datetime(2026, 8, 27, 12, 0).timestamp())

    # 年初看到"12月31日"：今年那次还没到，只能是去年的
    january = int(_datetime.datetime(2026, 1, 10, 12, 0).timestamp())
    assert parse_relative_time("12月31日", now=january) == int(
        _datetime.datetime(2025, 12, 31, 12, 0).timestamp()
    )


def test_crawl_collects_first_page(monkeypatch) -> None:
    browser = FakeBrowser(
        probes=[{"rendered": 40, "total": 185}],
        collects=[[_card("BV1aaaaaaaaa", "标题A", "4小时前"), _card("BV1bbbbbbbbb", "标题B", "2天前")]],
        next_results=[False],
    )
    install(monkeypatch, browser)

    result = crawl_space_page(
        "space.bilibili.com/3546865492560315/upload/video",
        max_pages=4,
        sleep=NOSLEEP,
    )

    assert len(result.videos) == 2
    assert result.total == 185
    assert result.attempts == 1
    assert result.videos[0].title == "标题A", "新发布的排最前"
    assert result.videos[0].created > 0, "相对时间要换算成时间戳，否则缓存排序失效"


def test_crawl_reopens_page_when_first_render_fails(monkeypatch) -> None:
    """第 1 次打开没渲染出列表 → 重开一次，第 2 次成功。"""
    browser = FakeBrowser(
        probes=[{"rendered": 0, "total": 185}] * 12 + [{"rendered": 40, "total": 185}],
        collects=[[_card("BV1ccccccccc", "标题C")]],
        next_results=[False],
    )
    install(monkeypatch, browser)

    result = crawl_space_page(
        "https://space.bilibili.com/1/upload/video",
        max_attempts=4,
        max_pages=3,
        sleep=NOSLEEP,
    )

    assert result.attempts == 2
    assert len(browser.navigations) == 2
    assert len(result.videos) == 1


def test_crawl_says_blocked_not_empty_when_total_is_present(monkeypatch) -> None:
    """页面写着 185 却一条都没渲染 = 数据请求被拦，绝不能报成"这个 UP 主没视频"。"""
    browser = FakeBrowser(probes=[{"rendered": 0, "total": 185}])
    install(monkeypatch, browser)

    result = crawl_space_page(
        "https://space.bilibili.com/1/upload/video",
        max_attempts=3,
        sleep=NOSLEEP,
    )

    assert result.videos == []
    assert result.total == 185
    assert "被拦" in result.message
    assert "185" in result.message
    assert "没有投稿" not in result.message, "不能把数据请求失败报成 UP 主没视频"
    assert len(browser.navigations) == 3


def test_crawl_reports_no_videos_when_page_has_no_total(monkeypatch) -> None:
    """页面既没总数也没列表：才是真的"没有投稿"。"""
    browser = FakeBrowser(probes=[{"rendered": 0, "total": 0}])
    install(monkeypatch, browser)

    result = crawl_space_page(
        "https://space.bilibili.com/1/upload/video",
        max_attempts=2,
        sleep=NOSLEEP,
    )

    assert result.videos == []
    assert "没有投稿" in result.message


def test_crawl_clicks_through_pages(monkeypatch) -> None:
    """光滚动停在第 1 页——必须逐页点分页器，每页的卡片都累计。"""
    browser = FakeBrowser(
        probes=[{"rendered": 40, "total": 185}],
        collects=[
            [_card("BV1page00001"), _card("BV1page00002")],
            [_card("BV1page00003"), _card("BV1page00004")],
            [_card("BV1page00005")],
        ],
        next_results=[True, True, False],
    )
    install(monkeypatch, browser)

    result = crawl_space_page(
        "https://space.bilibili.com/1/upload/video",
        max_pages=6,
        sleep=NOSLEEP,
    )

    assert browser.next_clicks >= 2, "要真的去点下一页"
    assert {video.bvid for video in result.videos} == {
        "BV1page00001", "BV1page00002", "BV1page00003", "BV1page00004", "BV1page00005",
    }


def test_crawl_prefers_api_items_over_relative_time(monkeypatch) -> None:
    """接口响应有精确 created 时间戳，比 DOM 的"4小时前"准——冲突时以接口为准。"""
    browser = FakeBrowser(
        probes=[{"rendered": 40, "total": 185}],
        collects=[[_card("BV1aaaaaaaaa", "DOM 里的标题", "4小时前")]],
        api_pages=[[_api_item("BV1aaaaaaaaa", "接口给的标题", created=1_791_028_200)]],
        next_results=[False],
    )
    install(monkeypatch, browser)

    result = crawl_space_page(
        "https://space.bilibili.com/1/upload/video",
        max_pages=3,
        sleep=NOSLEEP,
    )

    assert len(result.videos) == 1
    assert result.videos[0].title == "接口给的标题"
    assert result.videos[0].created == 1_791_028_200


def test_crawl_merges_into_existing_cache(monkeypatch, tmp_path) -> None:
    """两条路线共用一份地址库：抓取不能把接口路线已存的地址覆盖掉。"""
    from backend.bili_space import UpVideo, load_cache, save_cache

    save_cache(
        tmp_path, 7,
        [UpVideo(bvid="BV1api00000001", url="https://www.bilibili.com/video/BV1api00000001",
                 title="接口路线抓的", created=1700000000)],
        complete=True, fetched_pages=1, total=2,
    )

    browser = FakeBrowser(
        probes=[{"rendered": 40, "total": 185}],
        collects=[[_card("BV1paged0001", "页面抓的")]],
        next_results=[False],
    )
    install(monkeypatch, browser)

    crawl_space_page(
        "https://space.bilibili.com/7/upload/video",
        max_pages=3,
        sleep=NOSLEEP,
        cache_dir=tmp_path,
    )

    cached = load_cache(tmp_path, 7)
    assert cached is not None
    assert {video["bvid"] for video in cached["videos"]} == {
        "BV1api00000001",
        "BV1paged0001",
    }, "接口路线与页面路线的地址必须并存在同一份缓存里"


def test_crawl_without_playwright_gives_install_hint(monkeypatch) -> None:
    """没装 Chromium 时要告诉用户怎么装，而不是甩一个 import 错误。"""
    def explode():
        raise RuntimeError("Executable doesn't exist at /nope/chrome")

    monkeypatch.setattr(space_page, "_open", explode)

    result = crawl_space_page("space.bilibili.com/1/upload/video", sleep=NOSLEEP)

    assert result.videos == []
    assert "playwright install chromium" in result.message


def test_complete_uses_merged_count_not_this_run(monkeypatch, tmp_path) -> None:
    """本地已有 180 条、这次只补到 5 条：合并后已满 185，缓存就该标 complete。

    用"本次条数"判断会让缓存永远停在不完整，下次接口路线会白白走一遍续传。
    """
    from backend.bili_space import UpVideo, load_cache, save_cache

    save_cache(
        tmp_path, 9,
        [UpVideo(bvid=f"BV1old{i:06d}", url=f"https://www.bilibili.com/video/BV1old{i:06d}",
                 created=1700000000 + i) for i in range(180)],
        complete=True, fetched_pages=5, total=185,
    )

    browser = FakeBrowser(
        probes=[{"rendered": 40, "total": 185}],
        collects=[[_card(f"{_bvid(f'new{i:02d}')}") for i in range(5)]],
        next_results=[False],
    )
    install(monkeypatch, browser)

    crawl_space_page(
        "https://space.bilibili.com/9/upload/video",
        max_pages=3,
        sleep=NOSLEEP,
        cache_dir=tmp_path,
    )

    cached = load_cache(tmp_path, 9)
    assert cached is not None
    assert len(cached["videos"]) == 185
    assert cached["complete"] is True, "合并后已到 total，必须标完整"


def _seed_cache(tmp_path, uid: int, *, count: int, total: int, complete: bool,
                missing_pages: list[int] | None = None) -> None:
    from backend.bili_space import UpVideo, save_cache

    save_cache(
        tmp_path, uid,
        [UpVideo(bvid=f"{_bvid(f'seed{i:02d}')}", url=f"https://www.bilibili.com/video/{_bvid(f'seed{i:02d}')}",
                 title=f"旧{i}", created=1700000000 + i) for i in range(count)],
        complete=complete, fetched_pages=5, total=total, missing_pages=missing_pages,
    )


def test_crawl_only_checks_first_page_when_cache_is_complete(monkeypatch, tmp_path) -> None:
    """本地已完整且够数：新视频只会出现在最前，只点第 1 页就够，不该再翻 5 页。"""
    _seed_cache(tmp_path, 1, count=185, total=185, complete=True)
    known = [
        {"bvid": f"{_bvid(f'seed{i:02d}')}", "url": f"https://www.bilibili.com/video/{_bvid(f'seed{i:02d}')}",
         "title": "旧", "published": "2026-01-01"} for i in range(40)
    ]
    browser = FakeBrowser(
        probes=[{"rendered": 40, "total": 185}],
        collects=[known],
        next_results=[False],
    )
    install(monkeypatch, browser)

    result = crawl_space_page(
        "https://space.bilibili.com/1/upload/video",
        max_pages=5, sleep=NOSLEEP, cache_dir=tmp_path,
    )

    assert browser.clicked_pages == [], "本地已完整就只该看第 1 页，不该翻页"
    assert result.rounds == 1, "只发一个请求就该够"
    assert result.added == 0
    assert len(result.videos) == 185, (
        "返回的必须是本地合并后的全量，不是本次抓到的那 40 条——"
        "否则用户每次都看到「只拉了 40 个」"
    )
    assert "本地已完整" in result.message


def test_crawl_visits_only_recorded_missing_pages(monkeypatch, tmp_path) -> None:
    """上次缺第 2 页：这次直奔第 2 页，不再把 1..5 全点一遍。"""
    _seed_cache(tmp_path, 1, count=145, total=185, complete=False, missing_pages=[2])
    browser = FakeBrowser(
        probes=[{"rendered": 40, "total": 185}],
        collects=[[_card(_bvid("new"))]],
        next_results=[False],
    )
    install(monkeypatch, browser)

    crawl_space_page(
        "https://space.bilibili.com/1/upload/video",
        max_pages=5, sleep=NOSLEEP, cache_dir=tmp_path,
    )

    assert browser.clicked_pages == ["2"], f"只该点记录的缺页，实际点了 {browser.clicked_pages}"
    assert browser.next_clicks == 1


def test_crawl_records_missing_page_for_the_next_run(monkeypatch, tmp_path) -> None:
    """某页被拦：写进缓存的 missing_pages，下次直接补它。"""
    from backend.bili_space import load_cache

    browser = FakeBrowser(
        probes=[{"rendered": 40, "total": 80}],
        collects=[[_card(_bvid("first"))], []],  # 第 2 页一条都没渲染出来
        next_results=[True],  # 页码点得到，但那一页是空的
    )
    install(monkeypatch, browser)

    result = crawl_space_page(
        "https://space.bilibili.com/1/upload/video",
        max_pages=5, sleep=NOSLEEP, cache_dir=tmp_path,
    )

    cached = load_cache(tmp_path, 1)
    assert cached is not None, f"缓存没写下来：{result.message}"
    assert cached.get("missing_pages") == [2], "被拦的页必须记下来"
    assert cached["complete"] is False


def test_crawl_clears_missing_pages_once_fetched(monkeypatch, tmp_path) -> None:
    """补到缺页后必须清掉记录，否则下次还会白跑一趟。"""
    from backend.bili_space import load_cache

    _seed_cache(tmp_path, 1, count=40, total=80, complete=False, missing_pages=[2])
    browser = FakeBrowser(
        probes=[{"rendered": 40, "total": 80}],
        collects=[[_card(_bvid("second"))]],
        next_results=[True],  # 页码点得到，但那一页是空的
    )
    install(monkeypatch, browser)

    crawl_space_page(
        "https://space.bilibili.com/1/upload/video",
        max_pages=5, sleep=NOSLEEP, cache_dir=tmp_path,
    )

    cached = load_cache(tmp_path, 1)
    assert cached is not None
    assert cached.get("missing_pages") == []
    assert browser.clicked_pages == ["2"]


def test_rendered_but_all_known_is_not_a_missing_page(monkeypatch, tmp_path) -> None:
    """页面渲染出来了、只是本地全都有 → 不是缺页，不能记进 missing_pages。

    把这两者混为一谈会让下次白跑一趟去"补"根本没缺的页。
    """
    from backend.bili_space import load_cache

    _seed_cache(tmp_path, 1, count=180, total=185, complete=False)
    known_page = [
        {"bvid": f"{_bvid(f'seed{i:02d}')}", "url": f"https://www.bilibili.com/video/{_bvid(f'seed{i:02d}')}",
         "title": "旧", "published": "2026-01-01"} for i in range(40)
    ]
    browser = FakeBrowser(
        probes=[{"rendered": 40, "total": 185}],
        collects=[known_page, known_page],
        next_results=[False],
    )
    install(monkeypatch, browser)

    crawl_space_page(
        "https://space.bilibili.com/1/upload/video",
        max_pages=5, sleep=NOSLEEP, cache_dir=tmp_path,
    )

    cached = load_cache(tmp_path, 1)
    assert cached is not None
    assert cached.get("missing_pages") == [], "渲染成功就不算缺页"


def test_parse_space_target_recognizes_season_link() -> None:
    """合集链接必须识别出合集号——丢掉它就会抓成「该 UP 的全部投稿」。"""
    from backend.space_page import parse_space_target

    target = parse_space_target("https://space.bilibili.com/404096387/lists/7817323?type=season")
    assert target.uid == "404096387"
    assert target.season_id == "7817323"
    assert target.is_season is True
    assert target.url == "https://space.bilibili.com/404096387/lists/7817323?type=season"

    uploads = parse_space_target("https://space.bilibili.com/404096387/upload/video")
    assert uploads.season_id is None
    assert uploads.kind == "uploads"

    # 同一个旧函数仍然按老规矩办事（只认 UID），别因为它改了就影响已有调用方
    assert parse_space_input("https://space.bilibili.com/404096387/lists/7817323?type=season") == (
        "https://space.bilibili.com/404096387/upload/video",
        "404096387",
    )


def test_season_cache_is_separate_from_uploads_cache(tmp_path) -> None:
    """合集缓存必须与投稿缓存分文件。

    合集只是该 UP 投稿的子集（实测 97/239）：混进同一份，"全量已拉齐"会误判，
    两条路线还会互相覆盖。
    """
    from backend.bili_space import UpVideo, load_cache, save_cache

    def video(seed: str) -> UpVideo:
        return UpVideo(bvid=_bvid(seed), url=f"https://www.bilibili.com/video/{_bvid(seed)}",
                       title=seed, created=1700000000)

    save_cache(tmp_path, 404096387, [video("up1")], complete=False, fetched_pages=6, total=239)
    save_cache(tmp_path, 404096387, [video("s1")], complete=True, fetched_pages=4,
               total=97, season_id="7817323")

    uploads = load_cache(tmp_path, 404096387)
    season = load_cache(tmp_path, 404096387, "7817323")
    assert uploads is not None and uploads["complete"] is False, "合集的 complete 不能算到投稿头上"
    assert season is not None and season["complete"] is True

    names = sorted(p.name for p in (tmp_path / "up_lists").iterdir())
    assert names == ["404096387.json", "404096387_season_7817323.json"]


def test_crawl_season_link_opens_and_caches_season_page(monkeypatch, tmp_path) -> None:
    """贴合集链接：打开的是合集页、写的是合集缓存、message 里带合集名。"""
    from backend.bili_space import load_cache

    browser = FakeBrowser(
        probes=[{"rendered": 30, "total": 97, "pageCount": 4, "seasonTitle": "指数测评合集"}],
        collects=[[_card(_bvid("s1"))]],
        next_results=[False],
    )
    install(monkeypatch, browser)

    result = crawl_space_page(
        "https://space.bilibili.com/404096387/lists/7817323?type=season",
        max_pages=5, sleep=NOSLEEP, cache_dir=tmp_path,
    )

    assert browser.navigations[0].endswith("/lists/7817323?type=season"), "打开的必须是合集页"
    assert (tmp_path / "up_lists" / "404096387_season_7817323.json").is_file()
    assert not (tmp_path / "up_lists" / "404096387.json").exists(), "不该动投稿缓存"
    assert "指数测评合集" in result.message
    assert result.total == 97, "总数应从「共 4 页 / 97 个」读出，而不是「视频 9月24日」里的 9"
    cached = load_cache(tmp_path, 404096387, "7817323")
    assert cached is not None and cached["total"] == 97
    # fake 只给了 1 条卡片（1 < 97），本来就不完整——complete 按合并后的总数算
    assert cached["complete"] is False


def test_harvest_backfills_api_fields_when_they_arrive_late(monkeypatch) -> None:
    """接口响应比 DOM 晚到：先被 DOM 占位，之后必须补回接口里的时长与精确时间。

    真机上 Playwright 的 response 回调是在 evaluate 期间才 flush 的，所以
    `_harvest` 第一次读接口通常是空的——不补的话，97 条会全部停在 DOM 的
    残缺版本上（合集卡片根本没有时长，时间也只是「3月30日」）。
    """
    bvid = _bvid("late")
    browser = FakeBrowser(
        probes=[{"rendered": 40, "total": 185}],
        collects=[[{"bvid": bvid, "url": f"https://www.bilibili.com/video/{bvid}",
                    "title": "DOM 版标题", "published": "3月30日"}]],
        next_results=[False],
    )
    install(monkeypatch, browser)

    calls = {"n": 0}
    late_api = [{
        "bvid": bvid, "title": "接口版标题", "created": 1774876497,
        "duration": "08:10", "author": "UP",
    }]

    def api_arrives_after_dom(_session):
        calls["n"] += 1
        # 第一次调用（DOM evaluate 之前）接口还没回来
        return [] if calls["n"] == 1 else late_api

    monkeypatch.setattr(space_page, "_api_items", api_arrives_after_dom)

    result = crawl_space_page("https://space.bilibili.com/1/upload/video",
                              max_pages=5, sleep=NOSLEEP)

    assert calls["n"] >= 2, "至少要读两次接口：DOM 之前一次、之后一次"
    video = next(v for v in result.videos if v.bvid == bvid)
    assert video.duration == "08:10", "接口晚到必须把时长补回来"
    assert video.created == 1774876497, "接口的精确时间戳优先于「3月30日」推算值"
    assert video.title == "接口版标题"


def test_api_rejects_bad_space_url() -> None:
    client = TestClient(main.app)

    response = client.post("/api/bili-space-crawl", json={"url": "https://example.com/not-bilibili"})

    assert response.status_code == 400
    assert "空间链接" in response.json()["detail"] or "UID" in response.json()["detail"]
