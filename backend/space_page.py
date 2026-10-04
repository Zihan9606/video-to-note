"""用 Playwright 抓 B 站 space 页面的视频地址（与 API 路线互补，不替代它）。

为什么不复用 `bili_space.py` 的直连接口：那条路未登录时按 IP 限流，连续翻页会吃
`412 / -352 / -403` 并要冷却。本路线用 Playwright 打开真实 Chromium 加载 space 页面，
走页面自己那套请求（完整指纹、页面来源、页面自己的 cookie），**实测第一页稳定 200**。

两条数据来源，互为兜底：

1. **拦截页面自己的 `x/space/wbi/arc/search` 响应**——字段最全（bvid / 标题 /
   `created` 精确时间戳 / 时长），翻页时页面自己会再发一次，同样拦得到；
2. **DOM 卡片**（`.bili-video-card`）——响应没拦到时兜底，只有相对时间（"4小时前"）。

翻页方式是页面自带的分页器（`下一页` 按钮），不是无限滚动——所以**必须逐页点击**，
光滚动到底只会停在第 1 页。

两个由实测得出的硬性约定：

- 页面数据请求偶发失败时，B 站把列表显示成"空间主人还没投过视频"（顶部却写着
  "视频 185"）。因此必须用页面总数校验：总数 > 0 却一条都没渲染出来，是抓取失败，
  要重开页面重试，绝不能报成"这个 UP 主没视频"。
- Playwright 的 `page.evaluate` 不认裸 `return`（那是 WebDriver 的写法），所以本模块
  的所有页面脚本都由 `_js` 统一包成箭头函数。
"""
from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .bili_space import UpVideo, _video_from_cache, load_cache, save_cache

LOGGER = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_MAX_PAGES = 12
DEFAULT_WAIT_SECONDS = 2.0
DEFAULT_PAGE_LOAD_WAIT = 5.0

SPACE_URL_RE = re.compile(r"space\.bilibili\.com/(?P<uid>\d+)", re.I)
BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")

ABSOLUTE_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
# 当年内的老视频只写月日，不带年份（8月27日）
CN_DATE_RE = re.compile(r"^(\d{1,2})\s*月\s*(\d{1,2})\s*日")
RELATIVE_UNITS = (
    (re.compile(r"(\d+)\s*秒前"), 1),
    (re.compile(r"(\d+)\s*分钟前"), 60),
    (re.compile(r"(\d+)\s*小时前"), 3600),
    (re.compile(r"(\d+)\s*天前"), 86400),
    (re.compile(r"(\d+)\s*个月前"), 30 * 86400),
    (re.compile(r"(\d+)\s*年前"), 365 * 86400),
)


# ---------------------------------------------------------------------------
# 输入解析
# ---------------------------------------------------------------------------


def parse_space_input(value: str) -> tuple[str, str]:
    """把用户输入解析成 (页面 URL, UID)。

    接受完整空间链接（含 /upload/video 等子路径）、纯 UID；识别不了就抛错，
    绝不猜一个地址出来。
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("请粘贴空间链接或填写 UID")
    match = SPACE_URL_RE.search(text)
    if match:
        uid = match.group("uid")
        return f"https://space.bilibili.com/{uid}/upload/video", uid
    if text.isdigit():
        return f"https://space.bilibili.com/{text}/upload/video", text
    if "space.bilibili.com" in text:
        raise ValueError("这个空间链接里没有 UID，形如 space.bilibili.com/<数字>")
    raise ValueError("请粘贴形如 space.bilibili.com/123456/upload/video 的链接，或纯数字 UID")


def parse_relative_time(text: str, *, now: float | None = None) -> int:
    """把卡片上的时间换算成 unix 时间戳：`4小时前` / `8月27日` / `2025-01-05`。

    B 站近期用相对时间、当年内用 `M月D日`（不带年份）、更早用完整日期。
    解析不出来返回 0——缓存按时间倒序，0 只会排到最后，不该因此丢掉这条视频。
    """
    import datetime as _datetime

    value = (text or "").strip()
    if not value:
        return 0
    base = time.time() if now is None else now

    absolute = ABSOLUTE_DATE_RE.match(value)
    if absolute:
        try:
            parsed = _datetime.datetime(
                int(absolute.group(1)), int(absolute.group(2)), int(absolute.group(3))
            )
            return int(parsed.replace(hour=12).timestamp())
        except ValueError:
            return 0

    chinese = CN_DATE_RE.match(value)
    if chinese:
        month, day = int(chinese.group(1)), int(chinese.group(2))
        this_year = _datetime.datetime.fromtimestamp(base).year
        for year in (this_year, this_year - 1):
            try:
                candidate = _datetime.datetime(year, month, day, 12)
            except ValueError:
                continue
            # 允许到明天：跨时区时"今天"的日历日可能比 UTC 快
            if candidate.timestamp() <= base + 86400:
                return int(candidate.timestamp())
        return 0

    for pattern, seconds in RELATIVE_UNITS:
        match = pattern.search(value)
        if match:
            return max(0, int(base) - int(match.group(1)) * seconds)
    if value.startswith("今天"):
        return max(0, int(base))
    if value.startswith("昨天"):
        return max(0, int(base) - 86400)
    return 0


# ---------------------------------------------------------------------------
# 页面脚本（裸 return，由 _js 统一包成箭头函数）
# ---------------------------------------------------------------------------

_PROBE_SCRIPT = r"""
const links = document.querySelectorAll('.bili-video-card a[href*="/video/BV"], a.bili-cover-card[href*="/video/BV"]');
const bodyText = document.body.innerText || '';
const totalMatch = bodyText.match(/视频\s*(\d+)/);
return {
    rendered: links.length,
    total: totalMatch ? Number(totalMatch[1]) : 0,
    ready: document.readyState,
};
"""

_COLLECT_SCRIPT = r"""
const out = [];
const seen = new Set();
for (const card of document.querySelectorAll('.bili-video-card')) {
    const a = card.querySelector('a[href*="/video/BV"]');
    if (!a) continue;
    const found = (a.href || '').match(/\/video\/(BV[0-9A-Za-z]{10})/);
    if (!found || seen.has(found[1])) continue;
    seen.add(found[1]);
    const titleEl = card.querySelector('.bili-video-card__title');
    const subEl = card.querySelector('.bili-video-card__subtitle');
    const durationEl = card.querySelector('[class*="duration" i]');
    out.push({
        bvid: found[1],
        url: 'https://www.bilibili.com/video/' + found[1],
        title: (titleEl && titleEl.textContent || '').trim(),
        published: (subEl && subEl.textContent || '').trim(),
        duration: (durationEl && durationEl.textContent || '').trim(),
    });
}
return out;
"""

# 分页器里点具体页码（`__PAGE_NO__` 由 _click_page 替换）。
# 用页码而不是"下一页"：能直接定位到没拿到的那一页重试，也能跳过已经拿到的。
# `// click-page` 是识别标记，测试按它区分脚本。
_CLICK_PAGE_TEMPLATE = r"""
// click-page
const target = String(__PAGE_NO__);
const buttons = Array.from(document.querySelectorAll('button'));
const btn = buttons.find(el => (el.innerText || '').trim() === target
    && /pagena|pagination/i.test(String(el.className || '')));
if (!btn) return false;
if (btn.disabled || /disabled/.test(String(btn.className || ''))) return false;
btn.click();
return true;
"""

_ACTIVE_PAGE_SCRIPT = r"""
const active = Array.from(document.querySelectorAll('button'))
    .find(el => /--active/.test(String(el.className || '')) && /^\d+$/.test((el.innerText || '').trim()));
return active ? Number((active.innerText || '').trim()) : 0;
"""

# 页面用的是 ps=40（与接口路线的 30 不同，别搞混）
SPACE_PAGE_SIZE = 40


# ---------------------------------------------------------------------------
# 浏览器会话（钩子，测试里替换成假实现）
# ---------------------------------------------------------------------------


@dataclass
class _Session:
    playwright: Any
    browser: Any
    page: Any
    captured: list[dict] = field(default_factory=list)


def _open() -> _Session:
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.launch(headless=False)
        page = browser.new_page()
    except Exception:
        playwright.stop()
        raise

    session = _Session(playwright=playwright, browser=browser, page=page)

    def on_response(response: Any) -> None:
        if "arc/search" not in response.url:
            return
        try:
            payload = response.json()
        except Exception:
            return
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return
        vlist = ((data.get("list") or {}).get("vlist")) or []
        page_info = data.get("page") or {}
        session.captured.append({
            "pn": page_info.get("pn"),
            "total": page_info.get("count"),
            "items": [
                {
                    "bvid": str(item.get("bvid") or ""),
                    "title": str(item.get("title") or ""),
                    "created": int(item.get("created") or 0),
                    "duration": str(item.get("length") or ""),
                    "author": str(item.get("author") or ""),
                }
                for item in vlist
                if isinstance(item, dict) and item.get("bvid")
            ],
        })

    page.on("response", on_response)
    return session


def _goto(session: _Session, url: str) -> None:
    session.page.goto(url, wait_until="domcontentloaded", timeout=60000)


def _js(session: _Session, script: str) -> Any:
    """执行页面脚本。Playwright 不认裸 return，这里统一包成箭头函数。"""
    return session.page.evaluate(f"() => {{ {script} }}")


def _close(session: _Session) -> None:
    try:
        session.browser.close()
    finally:
        session.playwright.stop()


def _api_items(session: _Session) -> list[dict[str, Any]]:
    """读取已拦到的接口条目（每翻一页都会新增）。"""
    items: list[dict[str, Any]] = []
    for entry in getattr(session, "captured", None) or []:
        items.extend(entry.get("items") or [])
    return items


def _click_page(session: _Session, page_no: int) -> bool:
    """点分页器上的页码；按钮不存在或不可点返回 False。"""
    return bool(_js(session, _CLICK_PAGE_TEMPLATE.replace("__PAGE_NO__", str(page_no))))


def _active_page(session: _Session) -> int:
    return int(_js(session, _ACTIVE_PAGE_SCRIPT) or 0)


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------


@dataclass
class SpacePageResult:
    """一次页面抓取的结果。`videos` 即便为空，`message` 也说清了为什么。"""

    url: str
    uid: str
    videos: list[UpVideo] = field(default_factory=list)
    total: int | None = None
    attempts: int = 0
    rounds: int = 0
    added: int = 0
    source: str = "space_page"
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "uid": self.uid,
            "videos": [video.as_dict() for video in self.videos],
            "count": len(self.videos),
            "total": self.total,
            "attempts": self.attempts,
            "rounds": self.rounds,
            "source": self.source,
            "message": self.message,
            # 与接口路线同名字段对齐：本次真正新增了几条（本地原本没有的）
            "added": self.added,
            "cached_count": None,
            "complete": bool(self.total is None or len(self.videos) >= self.total),
        }


def _video_from_api(item: dict[str, Any]) -> UpVideo | None:
    bvid = str(item.get("bvid") or "")
    if not BV_RE.fullmatch(bvid):
        return None
    return UpVideo(
        bvid=bvid,
        url=f"https://www.bilibili.com/video/{bvid}",
        title=str(item.get("title") or ""),
        created=int(item.get("created") or 0),
        duration=str(item.get("duration") or ""),
        author=str(item.get("author") or ""),
    )


def _video_from_dom(item: dict[str, Any]) -> UpVideo | None:
    bvid = str(item.get("bvid") or "")
    if not BV_RE.fullmatch(bvid):
        return None
    return UpVideo(
        bvid=bvid,
        url=str(item.get("url") or f"https://www.bilibili.com/video/{bvid}"),
        title=str(item.get("title") or ""),
        created=parse_relative_time(str(item.get("published") or "")),
        duration=str(item.get("duration") or ""),
    )


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------


def crawl_space_page(
    value: str,
    *,
    cache_dir: Path | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    max_pages: int = DEFAULT_MAX_PAGES,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> SpacePageResult:
    """打开 space 页面，逐页点击分页器，收集全部视频地址。

    与 `bili_space.fetch_up_videos` 一样会把结果并进本地缓存（同一个 uid），
    所以两条路线拿到的地址汇在同一份 `workspace/up_lists/<uid>.json` 里。
    """
    url, uid = parse_space_input(value)
    result = SpacePageResult(url=url, uid=uid)

    try:
        session = _open()
    except Exception as exc:  # 浏览器没装 / Playwright 没装
        result.message = (
            f"打不开浏览器：{exc}。首次使用需要先装 Chromium："
            "`.venv/bin/python -m playwright install chromium`"
        )
        return result

    collected: dict[str, UpVideo] = {}
    total: int | None = None
    rendered = 0

    try:
        for attempt in range(1, max_attempts + 1):
            result.attempts = attempt
            _goto(session, url)
            rendered = 0
            for _ in range(12):
                sleep(wait_seconds)
                probe = _js(session, _PROBE_SCRIPT) or {}
                rendered = int(probe.get("rendered") or 0)
                if probe.get("total"):
                    total = int(probe["total"])
                if rendered:
                    break
            if rendered:
                break
            if attempt < max_attempts:
                LOGGER.info("space 页面第 %s 次没渲染出列表，稍后重开（页面总数=%s）", attempt, total)
                sleep(wait_seconds * 2)

        if not rendered:
            if total:
                result.total = total
                result.message = (
                    f"页面打开了 {result.attempts} 次都没能取回列表：B 站数据请求被拦，"
                    f"过一会儿再试，或先扫码登录导入凭据。（页面显示该 UP 主共有 {total} 个视频）"
                )
            else:
                result.message = "页面上没有看到视频列表，这个空间可能没有投稿"
            return result

        # 逐页点页码收集。先按本地缓存算出"这次该点哪些页"——这是增量的核心：
        #   本地已完整且够数 → 只看第 1 页（新视频只会出现在最前），撞到全已知就收工
        #   本地记着缺页     → 只点缺的那几页（上一次被拦的第 2、4 页之类）
        #   首次 / 缺页未知   → 全部页走一遍
        previous = load_cache(cache_dir, int(uid)) if cache_dir else None
        known = {
            str(item.get("bvid") or "")
            for item in (previous or {}).get("videos") or []
            if isinstance(item, dict)
        }
        known.discard("")

        total_pages = math.ceil(total / SPACE_PAGE_SIZE) if total else max_pages
        total_pages = max(1, min(total_pages, max_pages))

        cached_complete = bool(previous and previous.get("complete")) and (
            not total or len(known) >= int(total)
        )
        planned_missing = sorted({
            int(page)
            for page in (previous or {}).get("missing_pages") or []
            if isinstance(page, int) and 1 <= int(page) <= total_pages
        })
        if cached_complete:
            target_pages = [1]
            stop_when_all_known = True
        elif planned_missing:
            target_pages = planned_missing
            stop_when_all_known = False  # 缺页可能在中间，不能见好就收
        else:
            target_pages = list(range(1, total_pages + 1))
            stop_when_all_known = False

        missing: list[int] = []
        for page_no in target_pages:
            result.rounds += 1
            if page_no > 1 and not _click_page(session, page_no):
                if planned_missing:
                    continue  # 记录的页码在分页器上不存在了（页数变了）
                break
            sleep(DEFAULT_PAGE_LOAD_WAIT)
            gained, rendered, page_total = _harvest(session, collected)
            if page_total:
                total = page_total
            if rendered == 0:
                if page_no > 1:
                    missing.append(page_no)
                continue
            if gained == 0 and stop_when_all_known and page_no > 1:
                break  # 这一页本地全有了：后面只会更旧

        # 首轮走完仍缺的页，再补一次
        for page_no in list(missing):
            result.rounds += 1
            if not _click_page(session, page_no):
                continue
            sleep(DEFAULT_PAGE_LOAD_WAIT)
            _gained, rendered, _page_total = _harvest(session, collected)
            if rendered:
                missing.remove(page_no)

    except Exception as exc:
        result.message = f"浏览器抓取中断：{exc}"
        return result
    finally:
        _close(session)

    result.total = total

    # ---- 与接口路线对齐：返回**本地合并后的完整视图**，不是本次抓到的子集 ----
    # 否则增量（本地已完整、只查第 1 页）时 count=40，用户看到"每次都只拉 40 个"，
    # 明明本地已经存了 185 条。存储可以增量累加，展示必须始终是全量。
    stored: dict[str, UpVideo] = {}
    for item in (previous or {}).get("videos") or []:
        video = _video_from_cache(item)
        if video:
            stored[video.bvid] = video
    stored.update(collected)

    result.videos = sorted(stored.values(), key=lambda video: video.created, reverse=True)
    # 本地原本没有的那些才是"本次新增"——接口路线的 added 是同一个语义
    result.added = sum(1 for bvid in collected if bvid not in known)

    if not result.videos:
        result.message = result.message or "没有抓到任何视频地址"
        return result

    fetched_now = len(collected)
    if cache_dir:
        # save_cache 内部会与 previous 合并，这里只投本次抓到的；complete 按合并后的算，
        # 否则这次只补 145 条、本地原有 40 条，合计已满却仍被标成"不完整"。
        complete = bool(result.total) and len(stored) >= int(result.total)
        saved = save_cache(
            cache_dir,
            int(uid),
            list(collected.values()),
            complete=complete,
            fetched_pages=max(1, (fetched_now + SPACE_PAGE_SIZE - 1) // SPACE_PAGE_SIZE),
            total=result.total,
            previous=previous,
            # 把这次还缺的页记下来：下次抓取只补这几页，不用再从第 1 页全翻一遍
            missing_pages=missing,
        )
        count = len((saved or {}).get("videos") or [])
        suffix = f"，已并入本地缓存（现有 {count} 条）"
    else:
        suffix = ""

    result.message = (
        f"本次页面抓取 {fetched_now} 条"
        + (f"，本地合计 {len(result.videos)} 条" if len(result.videos) != fetched_now else "")
        + (f"（页面显示共 {result.total} 条）" if result.total else "")
        + f"，翻了 {result.rounds} 页{suffix}"
    )
    if missing:
        result.message += (
            f"；缺第 {','.join(str(page) for page in missing)} 页（数据请求被拦），"
            "已记下，下次抓取会直接补这几页"
        )
    elif cached_complete:
        result.message += "；本地已完整，本次只查了有没有新视频"
    elif result.total and len(result.videos) < result.total:
        result.message += f"；还有 {result.total - len(result.videos)} 条没拿到，再抓一次可继续补齐"
    return result


def _harvest(
    session: _Session, collected: dict[str, UpVideo]
) -> tuple[int, int, int | None]:
    """收集当前页，返回 (本地新增条数, 本页 DOM 渲染出的条数, 页面声明的总数)。

    `本页 DOM 渲染出的条数` 是区分两种失败的关键：

    - 渲染 0 条 = 这一页的数据请求被拦了（真·缺页，要记下来补）
    - 渲染了但本地新增 0 条 = 这一页全是本地已有的视频（增量已经够了，可以收工）

    只看"新增数"会把这两者混为一谈，于是增量时把好好的一页当成被拦。
    接口响应是**累积**的（session 里只增不减），所以只用它补字段，不参与计数。
    """
    before = len(collected)
    page_total: int | None = None

    for item in _api_items(session):
        video = _video_from_api(item)
        if video and video.bvid not in collected:
            collected[video.bvid] = video

    rendered = 0
    for item in _js(session, _COLLECT_SCRIPT) or []:
        if not isinstance(item, dict):
            continue
        video = _video_from_dom(item)
        if not video:
            continue
        rendered += 1
        if video.bvid not in collected:
            collected[video.bvid] = video

    probe = _js(session, _PROBE_SCRIPT) or {}
    if probe.get("total"):
        page_total = int(probe["total"])
    return len(collected) - before, rendered, page_total
