"""按 UID 拉取 B 站 UP 主投稿列表（供后续批量转写用）。

两条路线，按可用性自动降级：

1. **直连 space 投稿接口** `x/space/wbi/arc/search`——字段最全（标题、投稿时间、
   时长、总数），且天然按页返回，可以控制页间延迟、可以续传。
   需要 wbi 签名 + 浏览器指纹参数（`dm_*`），未登录时风控很严。
2. **yt-dlp**（项目已有依赖）——它内部同样走这个接口，但 wbi 与指纹的实现更完整，
   作为降级用；缺点是 flat 模式只有 BV 号与地址，没有标题和时间。

**风控是 IP 级的**：连续请求会吃到 `412 / -352 / -403`，并且要冷却一段时间。
因此本模块的硬性约定是：

- 页间强制延迟（`page_delay`），重试走指数退避；
- 失败时**返回已拿到的部分**并标 `complete=False`，绝不整批丢弃——用户可以稍后
  用 `start_page` 续传；
- 报错里带上"是风控还是参数错"，让上层能给出可操作提示。
"""
from __future__ import annotations

import base64
import json
import logging
import math
import random
import string
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .video_processor import VideoProcessor

LOGGER = logging.getLogger(__name__)

SPACE_API = "https://api.bilibili.com/x/space/wbi/arc/search"
PAGE_SIZE = 30
DEFAULT_PAGE_DELAY = 2.5
DEFAULT_MAX_RETRIES = 3
# 单次调用最多翻多少页：一次把整本投稿拉到底几乎必然触发风控，
# 所以默认给一批（300 条），剩下用返回的 next_page 续传
DEFAULT_MAX_PAGES = 10
RISK_MARKERS = ("412", "-352", "-401", "-403", "风控", "频繁", "risk", "forbidden")


@dataclass
class UpVideo:
    bvid: str
    url: str
    title: str = ""
    created: int = 0
    duration: str = ""
    author: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "bvid": self.bvid,
            "url": self.url,
            "title": self.title,
            "created": self.created,
            "upload_date": (
                time.strftime("%Y-%m-%d", time.localtime(self.created))
                if self.created
                else ""
            ),
            "duration": self.duration,
            "author": self.author,
        }


@dataclass
class UpVideoPage:
    """一次调用的结果。`complete=False` 时 `videos` 仍是已拿到的有效数据。"""

    uid: int
    videos: list[UpVideo] = field(default_factory=list)
    total: int | None = None
    page: int = 1
    fetched_pages: int = 0
    next_page: int | None = None
    complete: bool = False
    source: str = ""
    message: str = ""
    # 增量更新：本次新入库几条、本地缓存现在共几条、上次更新时间
    added: int = 0
    cached_count: int | None = None
    cached_updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "videos": [video.as_dict() for video in self.videos],
            "count": len(self.videos),
            "total": self.total,
            "page": self.page,
            "fetched_pages": self.fetched_pages,
            "next_page": self.next_page,
            "complete": self.complete,
            "source": self.source,
            "message": self.message,
            "added": self.added,
            "cached_count": self.cached_count,
            "cached_updated_at": self.cached_updated_at,
        }


def _dm_params() -> dict[str, str]:
    """B 站的浏览器指纹参数（照 bili-user-fingerprint.min.js 的算法复刻）。

    真实浏览器的屏幕指纹在一整个会话里是稳定的，所以**同一轮拉取内复用同一套**，
    每页都重新随机反而像机器行为。
    """

    def get_wh(width: int = 1920, height: int = 1080) -> list[int]:
        rnd = math.floor(114 * random.random())
        return [2 * width + 2 * height + 3 * rnd, 4 * width - height + rnd, rnd]

    def get_of(scroll_top: int = 10, scroll_left: int = 10) -> list[int]:
        rnd = math.floor(514 * random.random())
        return [3 * scroll_top + 2 * scroll_left + rnd, 4 * scroll_top - 4 * scroll_left + 2 * rnd, rnd]

    return {
        "dm_img_list": "[]",
        "dm_img_str": base64.b64encode(
            "".join(random.choices(string.printable, k=32)).encode()
        )[:-2].decode(),
        "dm_cover_img_str": base64.b64encode(
            "".join(random.choices(string.printable, k=64)).encode()
        )[:-2].decode(),
        "dm_img_inter": json.dumps(
            {"ds": [], "wh": get_wh(), "of": get_of(random.randint(0, 100), 0)},
            separators=(",", ":"),
        ),
    }


def looks_like_risk_control(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in RISK_MARKERS)


def _space_page(
    uid: int,
    page_number: int,
    *,
    cookie: dict[str, str] | None,
    fingerprint: dict[str, str],
) -> dict[str, Any]:
    """拉一页投稿；返回接口的原始 data，失败时抛 RuntimeError。"""
    params: dict[str, Any] = {
        "keyword": "",
        "mid": uid,
        "order": "pubdate",
        "order_avoided": "true",
        "platform": "web",
        "pn": page_number,
        "ps": PAGE_SIZE,
        "tid": 0,
        "web_location": "333.1387",
        "special_type": "",
        "index": 0,
        **fingerprint,
    }
    signed = VideoProcessor._wbi_sign(params)
    url = f"{SPACE_API}?{urllib.parse.urlencode(signed)}"
    headers = dict(VideoProcessor._bili_headers(cookie))
    headers["Referer"] = f"https://space.bilibili.com/{uid}/video"
    headers["Origin"] = "https://space.bilibili.com"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=20
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc

    code = payload.get("code")
    if code != 0:
        raise RuntimeError(f"code={code} {payload.get('message') or ''}".strip())
    return payload.get("data") or {}


def _parse_space_page(data: dict[str, Any], uid: int) -> tuple[list[UpVideo], int | None]:
    page = data.get("page") or {}
    total = page.get("count")
    videos: list[UpVideo] = []
    for entry in (data.get("list") or {}).get("vlist") or []:
        bvid = str(entry.get("bvid") or "")
        if not bvid:
            continue
        videos.append(
            UpVideo(
                bvid=bvid,
                url=f"https://www.bilibili.com/video/{bvid}",
                title=str(entry.get("title") or ""),
                created=int(entry.get("created") or 0),
                duration=str(entry.get("length") or ""),
                author=str(entry.get("author") or ""),
            )
        )
    return videos, int(total) if isinstance(total, int) else None


def _yt_dlp_page(uid: int, *, limit: int | None = None) -> list[UpVideo]:
    """用 yt-dlp 取投稿（flat 模式：只有 BV 号与地址）。

    yt-dlp 内部从第 1 页开始翻到覆盖 `playlistend` 为止，所以**一次不要要太多**，
    否则请求量按批的序号平方增长，风控会先找上门；这里一次就是要整批，
    续传能力交给上面的 space 接口路线（它才是真正按页的）。
    """
    import yt_dlp

    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
    }
    if limit:
        options["playlistend"] = limit
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(f"https://space.bilibili.com/{uid}/video", download=False)
    videos: list[UpVideo] = []
    for entry in info.get("entries") or []:
        bvid = str(entry.get("id") or "")
        if not bvid or not bvid.startswith("BV"):
            continue
        videos.append(UpVideo(bvid=bvid, url=f"https://www.bilibili.com/video/{bvid}"))
    return videos


CACHE_DIR_NAME = "up_lists"


def _cache_path(cache_dir: Path | None, uid: int) -> Path | None:
    if not cache_dir:
        return None
    # UID 是调用方传进来的整数，已经在 fetch_up_videos 里转过 int，这里不再接受任意字符串
    return Path(cache_dir) / CACHE_DIR_NAME / f"{int(uid)}.json"


def load_cache(cache_dir: Path | None, uid: int) -> dict[str, Any] | None:
    """读本地缓存；文件损坏或格式不对一律当作没有缓存（绝不阻断拉取）。"""
    path = _cache_path(cache_dir, uid)
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("videos"), list):
        return None
    return payload


def _video_from_cache(item: Any) -> UpVideo | None:
    if not isinstance(item, dict):
        return None
    bvid = str(item.get("bvid") or "")
    if not bvid:
        return None
    return UpVideo(
        bvid=bvid,
        url=str(item.get("url") or f"https://www.bilibili.com/video/{bvid}"),
        title=str(item.get("title") or ""),
        created=int(item.get("created") or 0),
        duration=str(item.get("duration") or ""),
        author=str(item.get("author") or ""),
    )


def save_cache(
    cache_dir: Path | None,
    uid: int,
    videos: list[UpVideo],
    *,
    complete: bool,
    fetched_pages: int,
    total: int | None = None,
    previous: dict[str, Any] | None = None,
    missing_pages: list[int] | None = None,
) -> dict[str, Any] | None:
    """把结果并入本地缓存（按 bvid 去重、按投稿时间倒序），返回新缓存。

    `complete=False` 时也写：全量拉取本来就要分批，写下来下次才能续传补齐，
    断在半路就丢掉等于让用户从第 1 页重来一遍。

    `missing_pages` 是**页面抓取路线**记的离散缺页（它不像接口路线那样连续翻页，
    缺的可能是第 2、4 页）：显式传入就用传的；没传且本次已拉全则清空，否则保留
    上次记的——两条路线共用这份缓存，不能互相把对方的进度抹掉。
    """
    path = _cache_path(cache_dir, uid)
    if path is None:
        return None
    merged: dict[str, UpVideo] = {}
    if previous:
        for item in previous.get("videos") or []:
            video = _video_from_cache(item)
            if video:
                merged[video.bvid] = video
    for video in videos:
        merged[video.bvid] = video
    ordered = sorted(merged.values(), key=lambda video: video.created, reverse=True)

    previous_pages = int((previous or {}).get("fetched_pages") or 0)
    if missing_pages is None:
        resolved_missing = [] if complete else list((previous or {}).get("missing_pages") or [])
    else:
        resolved_missing = [int(page) for page in missing_pages]
    payload = {
        "uid": int(uid),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "complete": bool(complete),
        "fetched_pages": max(previous_pages, int(fetched_pages)),
        "total": total if total is not None else (previous or {}).get("total"),
        "missing_pages": resolved_missing,
        "videos": [video.as_dict() for video in ordered],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)
    except OSError:
        # 缓存写不进去只影响下次增量的速度，不该让这次已经拿到的数据失败
        LOGGER.warning("UP 主投稿缓存写入失败：%s", path, exc_info=True)
        return previous
    return payload


def _walk_pages(
    uid: int,
    *,
    mode: str = "all",
    limit: int | None = None,
    start_page: int = 1,
    max_pages: int = DEFAULT_MAX_PAGES,
    page_delay: float = DEFAULT_PAGE_DELAY,
    max_retries: int = DEFAULT_MAX_RETRIES,
    cookie: dict[str, str] | None = None,
    stop_when: Callable[[list[UpVideo]], bool] | None = None,
) -> UpVideoPage:
    """翻页主循环。返回部分结果永远好过抛异常丢数据。

    mode="recent" 只要前 limit 条（B 站按投稿时间倒序，前 N 条即最近 N 条）；
    mode="all" 尽量拉满 `max_pages` 页，剩余用 `next_page` 续传；
    `stop_when` 给增量更新用：某一页满足条件就停（倒序保证后面都是更旧的）。
    """
    uid = int(uid)
    result = UpVideoPage(uid=uid, page=start_page)
    if mode == "recent" and limit and limit > 0:
        max_pages = min(max_pages, max(1, math.ceil(limit / PAGE_SIZE)))

    fingerprint = _dm_params()  # 一轮拉取共用一套指纹，避免"每页都换屏幕"
    page_number = start_page
    pages_done = 0
    last_error = ""

    while pages_done < max_pages:
        if mode == "recent" and limit and len(result.videos) >= limit:
            # 只要最近 N 条，拿够了就是完整结果，不该再报"还有后续"
            result.complete = True
            break
        if pages_done and page_delay:
            time.sleep(page_delay)

        data: dict[str, Any] | None = None
        for attempt in range(max_retries):
            try:
                data = _space_page(
                    uid, page_number, cookie=cookie, fingerprint=fingerprint
                )
                break
            except RuntimeError as exc:
                last_error = str(exc)
                if not looks_like_risk_control(last_error) or attempt == max_retries - 1:
                    break
                # 指纹/风控类失败：换一套指纹，指数退避后再试
                fingerprint = _dm_params()
                time.sleep(page_delay * (3**attempt))
        if data is None:
            if result.videos:
                break  # 已经有数据：把它交出去，而不是整批作废
            if pages_done == 0 and start_page == 1:
                return _fetch_up_videos_via_ytdlp(
                    uid, mode=mode, limit=limit, last_error=last_error
                )
            result.message = _risk_message(last_error)
            return result

        videos, total = _parse_space_page(data, uid)
        pages_done += 1
        page_number += 1
        if total is not None:
            result.total = total
        if not videos:
            result.complete = True  # 空页 = 已到末尾
            break
        result.videos.extend(videos)
        result.source = "space_api"
        if stop_when is not None and stop_when(videos):
            # 增量更新命中已知视频：后面全是旧的，没必要再翻
            result.complete = True
            break
        # 到了总数、或者这一页不满一页，都说明后面没有了
        total_pages = math.ceil(total / PAGE_SIZE) if total else None
        if (total_pages is not None and page_number > total_pages) or len(videos) < PAGE_SIZE:
            result.complete = True
            break

    result.fetched_pages = pages_done
    if mode == "recent" and limit and len(result.videos) >= limit:
        # 拿够 limit 就是完整结果；这里**不截断**——返回前要整页并进本地缓存，
        # 只存截断后的 N 条会让下一次"全部视频"缺一大块。
        result.complete = True
        result.next_page = None
    if not result.complete:
        result.next_page = page_number
        if not result.message:
            result.message = (
                f"已拿到前 {pages_done} 页，还有后续；"
                f"用 start_page={page_number} 继续"
            )
    result.message = result.message or "已拉取到列表末尾"
    return result


def _annotate_cache(result: UpVideoPage, cached: dict[str, Any] | None) -> None:
    if not cached:
        result.cached_count = None
        result.cached_updated_at = ""
        return
    result.cached_count = len(cached.get("videos") or [])
    result.cached_updated_at = str(cached.get("updated_at") or "")


def fetch_up_videos(
    uid: int,
    *,
    mode: str = "all",
    limit: int | None = None,
    start_page: int = 1,
    max_pages: int = DEFAULT_MAX_PAGES,
    page_delay: float = DEFAULT_PAGE_DELAY,
    max_retries: int = DEFAULT_MAX_RETRIES,
    cookie: dict[str, str] | None = None,
    cache_dir: Path | None = None,
) -> UpVideoPage:
    """拉取 UP 主投稿：**只有两个选项，共用同一套增量存储**。

    - ``mode="all"``：增量更新本地缓存后返回**全部**视频地址；
    - ``mode="recent"``（配 ``limit``）：增量更新本地缓存后返回**最近 N 条**。

    两个选项的目标一致——让 ``workspace/up_lists/<uid>.json`` 跟上最新，区别只是最后
    截取多少条。增量的三种情形：

      * 本地为空 → 按需拉全（all 拉到 ``max_pages`` 上限或到底；recent 拉到够 N 条）
      * 本地不完整 → 从断点续传补齐，不从第 1 页重来
      * 本地完整 → 从第 1 页查新增，撞到已知视频即停（通常一个请求）

    因此**任何一次调用都会顺手把本地存全**：首次贵，之后两种模式都只剩一两个请求，
    这正是绕开 B 站 IP 限流的关键。
    """
    uid = int(uid)
    cached = load_cache(cache_dir, uid)
    stored: dict[str, UpVideo] = {}
    for item in (cached or {}).get("videos") or []:
        video = _video_from_cache(item)
        if video:
            stored[video.bvid] = video
    known = set(stored)

    # ---- 决定这次怎么联网：全部差异都由"本地现在是什么状态"决定 ----
    scenario: str
    walked: UpVideoPage | None
    if cached and not cached.get("complete"):
        scenario = "resume"
        walked = _walk_pages(
            uid, mode=mode, limit=limit,
            start_page=int(cached.get("fetched_pages") or 0) + 1,
            max_pages=max_pages, page_delay=page_delay, max_retries=max_retries,
            cookie=cookie,
        )
    elif not cached:
        scenario = "first"
        walked = _walk_pages(
            uid, mode=mode, limit=limit, start_page=start_page,
            max_pages=max_pages, page_delay=page_delay, max_retries=max_retries,
            cookie=cookie,
        )
    elif mode == "recent" and limit and len(stored) < limit:
        # 本地已完整（即 UP 主的全部视频就这么多）却不够 N 条：无处可拉
        scenario = "enough"
        walked = None
    else:
        # 本地完整：查新增，倒序保证撞到第一个已知视频之后全是旧的
        scenario = "fresh"
        walked = _walk_pages(
            uid, mode="all", start_page=1, max_pages=max_pages,
            page_delay=page_delay, max_retries=max_retries, cookie=cookie,
            stop_when=lambda page: any(video.bvid in known for video in page),
        )

    # ---- 合并本地与新拉到的，再落盘 ----
    fetched = list(walked.videos) if walked else []
    added = [video for video in fetched if video.bvid not in known]
    for video in fetched:
        stored[video.bvid] = video

    saved: dict[str, Any] | None = None
    if cache_dir and walked is not None and (fetched or stored):
        saved = save_cache(
            cache_dir,
            uid,
            fetched,
            # 查新增不会让"已完整"的缓存变残缺；前两种情形以这次拉取的结论为准
            complete=walked.complete if scenario in {"first", "resume"} else True,
            fetched_pages=(walked.page - 1) + walked.fetched_pages,
            total=walked.total,
            previous=cached,
        )

    result = UpVideoPage(uid=uid, page=start_page)
    result.videos = sorted(stored.values(), key=lambda video: video.created, reverse=True)
    local_count = len(result.videos)
    if mode == "recent" and limit:
        result.videos = result.videos[:limit]
    result.added = len(added)
    result.source = walked.source if walked is not None else "cache"
    result.total = (
        walked.total if walked and walked.total is not None
        else (cached or {}).get("total")
    )
    _annotate_cache(result, saved if saved is not None else cached)

    if scenario == "enough":
        result.complete = True
        result.message = (
            f"本地已有 {local_count} 条，该 UP 主全部视频不足 {limit} 条，无需联网"
        )
        return result

    if scenario == "fresh":
        result.complete = True
        result.next_page = None
        if added:
            result.message = f"新增 {len(added)} 条，本地共 {local_count} 条"
        else:
            updated = str((saved or cached or {}).get("updated_at") or "")[:10]
            result.message = f"没有新视频：本地已有 {local_count} 条"
            if updated:
                result.message += f"（{updated} 更新过）"
        return result

    # first / resume：完全以这次拉取的结论为准，没拉到的部分要能续传
    result.complete = bool(walked and walked.complete)
    result.next_page = walked.next_page if walked else None
    result.fetched_pages = walked.fetched_pages if walked else 0
    result.message = (walked.message if walked else "") or ""
    if local_count and not result.complete and result.next_page:
        result.message = (
            f"已缓存 {local_count} 条、停在第 {result.next_page - 1} 页；"
            f"用 start_page={result.next_page} 继续，或稍后重试"
        )
    elif local_count:
        prefix = f"首次拉取：已缓存 {local_count} 条" if scenario == "first" else None
        result.message = prefix or f"已补齐到 {local_count} 条"
        if walked and walked.message and "风控" in walked.message:
            result.message = f"{result.message}（{walked.message}）"
    return result


def _fetch_up_videos_via_ytdlp(
    uid: int, *, mode: str, limit: int | None, last_error: str
) -> UpVideoPage:
    """space 接口整段不可用时的降级：交给 yt-dlp。"""
    result = UpVideoPage(uid=uid, source="yt_dlp")
    try:
        result.videos = _yt_dlp_page(uid, limit=limit if mode == "recent" else None)
    except Exception as exc:
        result.message = (
            f"两条路线都被 B 站拦下了：space 接口 {last_error or '不可用'}；"
            f"yt-dlp {exc}。这类限制是按 IP 限流的，过一段时间再试，"
            "或先用已保存的 B 站凭据（扫码登录导入）提高成功率。"
        )
        return result
    # yt-dlp 一次调用就是要整批：要么全拿到，要么抛异常走上面的分支
    result.complete = True
    if mode == "recent" and limit:
        result.videos = result.videos[:limit]
    if not any(video.title for video in result.videos):
        result.message = (
            "space 接口被风控，已改用 yt-dlp：只有 BV 号与地址，没有标题和投稿时间"
        )
    return result


def _risk_message(error: str) -> str:
    return (
        f"拉取被 B 站拦截（{error}）。这类限制按 IP 限流，过一段时间再试，"
        "或先扫码登录导入凭据提高成功率。"
    )
