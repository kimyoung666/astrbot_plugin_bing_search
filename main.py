"""AstrBot 必应网页与图片搜索插件。

在 cn.bing.com 上搜索公开网页（带标题 / 摘要 / 来源，支持时效筛选），
也可单独搜索并转发 1-5 张相关图片。

设计要点：
- 网页搜索与图片转发是**两项完全独立**的能力，网页搜索不会暗中附带图片。
- 图片仅在**工具调用期间**由插件直接发送到当前会话，AI 只收到不含图片直链的
  文字回执；命令路径通过 yield 返回图片链。
- 失败不泄密：日志与回执只记录稳定错误码，绝不回显 API Key 或远端错误正文。
"""

import asyncio
import html as _html
import json
import re
import urllib.parse

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 稳定错误码：仅用于日志与回执，不暴露远端细节
E_DISABLED = "E_DISABLED"          # 插件总开关关闭
E_WEB_DISABLED = "E_WEB_DISABLED"  # 网页搜索关闭
E_IMG_DISABLED = "E_IMG_DISABLED"  # 图片搜索关闭
E_EMPTY_QUERY = "E_EMPTY_QUERY"    # 关键词为空
E_TIMEOUT = "E_TIMEOUT"            # 请求超时
E_NETWORK = "E_NETWORK"            # 网络异常
E_HTTP = "E_HTTP"                  # 非 200 响应
E_PARSE = "E_PARSE"                # 解析异常
E_NO_RESULT = "E_NO_RESULT"        # 无结果
E_SEND_FAIL = "E_SEND_FAIL"        # 图片发送失败

# 时效筛选：Bing 的 filters=ex1:"ezX" 语法
_FRESHNESS_MAP = {
    "day": "ez1",
    "week": "ez2",
    "month": "ez3",
    "year": "ez4",
}

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_tags(raw: str) -> str:
    """去掉 HTML 标签并还原实体，压缩空白。"""
    if not raw:
        return ""
    text = _TAG_RE.sub("", raw)
    text = _html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _clamp(value, low, high, fallback):
    """安全地把配置/参数限定到 [low, high] 区间。"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, v))


class BingClient:
    """轻量 Bing 抓取客户端，仅依赖 aiohttp。"""

    def __init__(self, host: str, timeout: int):
        self.host = (host or "cn.bing.com").strip().strip("/")
        if self.host.startswith(("http://", "https://")):
            self.base = self.host.rstrip("/")
        else:
            self.base = f"https://{self.host}"
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": _DEFAULT_UA, "Accept-Language": "zh-CN,zh;q=0.9"},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _fetch(self, url: str) -> str:
        """发起 GET 请求，返回网页文本。异常统一抛给上层映射为错误码。"""
        session = await self._get_session()
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                raise BingHttpError(resp.status)
            return await resp.text(errors="ignore")

    async def search_web(self, query: str, count: int, freshness: str = "") -> list[dict]:
        """搜索网页，返回 [{title, url, snippet}]。"""
        params = {
            "q": query,
            "count": str(count),
            "setlang": "zh-CN",
            "ensearch": "0",
        }
        if freshness in _FRESHNESS_MAP:
            params["filters"] = f'ex1:"{_FRESHNESS_MAP[freshness]}"'
        url = f"{self.base}/search?" + urllib.parse.urlencode(params)
        body = await self._fetch(url)
        return self._parse_web(body, count)

    def _parse_web(self, body: str, count: int) -> list[dict]:
        results: list[dict] = []
        blocks = re.findall(r'<li class="b_algo".*?</li>', body, re.S)
        for block in blocks:
            link = re.search(
                r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S
            )
            if not link:
                continue
            url = _html.unescape(link.group(1))
            title = _strip_tags(link.group(2))
            snippet_m = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
            snippet = _strip_tags(snippet_m.group(1)) if snippet_m else ""
            if not title and not url:
                continue
            results.append({"title": title, "url": url, "snippet": snippet})
            if len(results) >= count:
                break
        return results

    async def search_images(self, query: str, count: int) -> list[dict]:
        """搜索图片，返回 [{murl, thumb, purl, title}]。"""
        params = {"q": query, "first": "1", "count": str(count)}
        url = f"{self.base}/images/search?" + urllib.parse.urlencode(params)
        body = await self._fetch(url)
        return self._parse_images(body, count)

    def _parse_images(self, body: str, count: int) -> list[dict]:
        results: list[dict] = []
        metas = re.findall(r'class="iusc"[^>]*m="([^"]+)"', body)
        for raw in metas:
            try:
                meta = json.loads(_html.unescape(raw))
            except (ValueError, TypeError):
                continue
            murl = meta.get("murl") or meta.get("turl")
            if not murl:
                continue
            results.append(
                {
                    "murl": murl,
                    "thumb": meta.get("turl", ""),
                    "purl": meta.get("purl", ""),
                    "title": meta.get("t", ""),
                }
            )
            if len(results) >= count:
                break
        return results


class BingHttpError(Exception):
    """HTTP 状态异常，携带状态码，便于日志分类。"""

    def __init__(self, status: int):
        self.status = status
        super().__init__(f"http {status}")


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------


class BingSearchPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._client: BingClient | None = None

    # -- 配置读取（每次实时读取，支持 WebUI 热更新） -------------------------

    def _cfg(self, key: str, default):
        val = self.config.get(key, default)
        return default if val is None else val

    def _want_url(self) -> bool:
        return bool(self._cfg("need_url", True))

    def _max_snippet(self) -> int:
        return _clamp(self._cfg("max_snippet_chars", 1200), 200, 8000, 1200)

    def _timeout(self) -> int:
        return _clamp(self._cfg("timeout_seconds", 30), 5, 120, 30)

    def _default_count(self) -> int:
        return _clamp(self._cfg("default_count", 5), 1, 50, 5)

    def _default_image_count(self) -> int:
        return _clamp(self._cfg("default_image_count", 3), 1, 5, 3)

    def _need_summary(self) -> bool:
        return bool(self._cfg("need_summary", True))

    def _enabled(self) -> bool:
        return bool(self._cfg("enabled", True))

    def _web_enabled(self) -> bool:
        return bool(self._cfg("enable_web_search", True))

    def _img_enabled(self) -> bool:
        return bool(self._cfg("enable_image_search", True))

    def _client_for(self) -> BingClient:
        host = str(self._cfg("bing_host", "cn.bing.com"))
        timeout = self._timeout()
        # host 或超时变化时重建客户端，保证配置热更新生效
        if (
            self._client is None
            or self._client.host != host.strip().strip("/")
            or self._client.timeout != timeout
        ):
            self._client = BingClient(host, timeout)
        return self._client

    # -- 统一抓取封装（错误码 + 日志脱敏） ----------------------------------

    async def _do_web(self, query: str, count: int, freshness: str = "") -> tuple[int, list]:
        """返回 (错误码或空串, 结果列表)。"""
        client = self._client_for()
        try:
            results = await client.search_web(query, count, freshness)
        except asyncio.TimeoutError:
            logger.warning("[bing] %s", E_TIMEOUT)
            return E_TIMEOUT, []
        except BingHttpError as exc:
            logger.warning("[bing] %s status=%s", E_HTTP, exc.status)
            return E_HTTP, []
        except aiohttp.ClientError:
            logger.warning("[bing] %s", E_NETWORK)
            return E_NETWORK, []
        except Exception:  # noqa: BLE001 —— 兜底，绝不外泄异常正文
            logger.warning("[bing] %s", E_PARSE, exc_info=True)
            return E_PARSE, []
        if not results:
            return E_NO_RESULT, []
        return "", results

    async def _do_images(self, query: str, count: int) -> tuple[int, list]:
        client = self._client_for()
        try:
            results = await client.search_images(query, count)
        except asyncio.TimeoutError:
            logger.warning("[bing] %s", E_TIMEOUT)
            return E_TIMEOUT, []
        except BingHttpError as exc:
            logger.warning("[bing] %s status=%s", E_HTTP, exc.status)
            return E_HTTP, []
        except aiohttp.ClientError:
            logger.warning("[bing] %s", E_NETWORK)
            return E_NETWORK, []
        except Exception:  # noqa: BLE001
            logger.warning("[bing] %s", E_PARSE, exc_info=True)
            return E_PARSE, []
        if not results:
            return E_NO_RESULT, []
        return "", results

    def _web_text(self, query: str, results: list) -> str:
        """把网页结果格式化为给 AI 的纯文本（不含任何图片）。"""
        want_url = self._want_url()
        want_summary = self._need_summary()
        limit = self._max_snippet()
        lines = [f"必应网页搜索结果（关键词：{query}，共 {len(results)} 条）："]
        for idx, item in enumerate(results, 1):
            lines.append(f"{idx}. 标题：{item['title'] or '（无标题）'}")
            if want_summary and item.get("snippet"):
                snippet = item["snippet"]
                if len(snippet) > limit:
                    snippet = snippet[:limit] + "…"
                lines.append(f"   摘要：{snippet}")
            if want_url and item.get("url"):
                lines.append(f"   来源：{item['url']}")
        return "\n".join(lines)

    # -- 命令：中文主命令 + 英文兼容 ----------------------------------------

    @filter.command("bing搜索")
    async def cmd_web_zh(self, event: AstrMessageEvent):
        """必应网页搜索（中文命令）"""
        async for r in self._cmd_web(event):
            yield r

    @filter.command("search")
    async def cmd_web_en(self, event: AstrMessageEvent):
        """必应网页搜索（英文兼容命令）"""
        async for r in self._cmd_web(event):
            yield r

    @filter.command("bing_search")
    async def cmd_web_en2(self, event: AstrMessageEvent):
        """必应网页搜索（英文兼容命令）"""
        async for r in self._cmd_web(event):
            yield r

    @filter.command("bing搜图")
    async def cmd_img_zh(self, event: AstrMessageEvent):
        """必应图片搜索（中文命令）"""
        async for r in self._cmd_image(event):
            yield r

    @filter.command("bing_image")
    async def cmd_img_en(self, event: AstrMessageEvent):
        """必应图片搜索（英文兼容命令）"""
        async for r in self._cmd_image(event):
            yield r

    def _extract_query(self, event: AstrMessageEvent) -> str:
        """从消息中去掉命令前缀，取剩余关键词。"""
        msg = (event.message_str or "").strip()
        if not msg:
            return ""
        # 去掉首个 token（命令本身）
        parts = msg.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    async def _cmd_web(self, event: AstrMessageEvent):
        if not self._enabled():
            yield event.plain_result("必应搜索插件当前已关闭。")
            return
        if not self._web_enabled():
            yield event.plain_result("网页搜索功能当前已关闭。")
            return
        query = self._extract_query(event)
        if not query:
            yield event.plain_result("用法：/bing搜索 <关键词>")
            return
        code, results = await self._do_web(query, self._default_count())
        if code:
            yield event.plain_result(self._error_text(code))
            return
        yield event.plain_result(self._web_text(query, results))

    async def _cmd_image(self, event: AstrMessageEvent):
        if not self._enabled():
            yield event.plain_result("必应搜索插件当前已关闭。")
            return
        if not self._img_enabled():
            yield event.plain_result("图片搜索功能当前已关闭。")
            return
        query = self._extract_query(event)
        if not query:
            yield event.plain_result("用法：/bing搜图 <关键词>")
            return
        code, results = await self._do_images(query, self._default_image_count())
        if code:
            yield event.plain_result(self._error_text(code))
            return
        chain = [Plain(f"必应图片搜索（关键词：{query}），共 {len(results)} 张：")]
        for item in results:
            chain.append(Image(item["murl"]))
        yield event.chain_result(chain)

    def _error_text(self, code: str) -> str:
        mapping = {
            E_TIMEOUT: "搜索请求超时，请稍后再试。",
            E_NETWORK: "网络异常，搜索请求失败。",
            E_HTTP: "搜索服务返回异常响应。",
            E_PARSE: "搜索结果解析失败。",
            E_NO_RESULT: "未找到相关结果。",
        }
        return mapping.get(code, "搜索失败，请稍后再试。")

    # -- LLM 工具 1：网页搜索（绝不附图） -----------------------------------

    @filter.llm_tool(name="web_search_bing")
    async def web_search_bing(self, event: AstrMessageEvent, query: str, count: int = 0, freshness: str = ""):
        """在必应（cn.bing.com）搜索公开网页，返回带标题、摘要与来源地址的结果列表。

        仅返回网页文本结果，不会附带或转发任何图片。

        Args:
            query(string): 搜索关键词，必填。
            count(number): 需要的结果条数，可选，1-50，默认使用插件配置值。
            freshness(string): 时效筛选，可选，取值 day/week/month/year 之一，留空表示不限。
        """
        if not self._enabled():
            yield event.plain_result(f"搜索失败（{E_DISABLED}）：插件已关闭。")
            return
        if not self._web_enabled():
            yield event.plain_result(f"搜索失败（{E_WEB_DISABLED}）：网页搜索已关闭。")
            return
        q = (query or "").strip()
        if not q:
            yield event.plain_result(f"搜索失败（{E_EMPTY_QUERY}）：关键词为空。")
            return
        n = _clamp(count, 1, 50, self._default_count()) if count else self._default_count()
        fresh = (freshness or "").strip().lower()
        code, results = await self._do_web(q, n, fresh)
        if code:
            yield event.plain_result(f"搜索失败（{code}）。")
            return
        yield event.plain_result(self._web_text(q, results))

    # -- LLM 工具 2：图片搜索（工具调用期间直接发图，回执不含直链） ---------

    @filter.llm_tool(name="image_search_bing")
    async def image_search_bing(self, event: AstrMessageEvent, query: str, count: int = 0):
        """在必应（cn.bing.com）搜索图片，并在本次工具调用期间直接把 1-5 张图片发送到当前对话。

        图片由插件直接发送给用户，调用方只会收到不含图片直链的文字回执
        （包含成功与失败的数量），无需也不能再次转发图片。

        Args:
            query(string): 搜索关键词，必填。
            count(number): 需要发送的图片数量，可选，1-5，默认使用插件配置值。
        """
        if not self._enabled():
            yield event.plain_result(f"搜图失败（{E_DISABLED}）：插件已关闭。")
            return
        if not self._img_enabled():
            yield event.plain_result(f"搜图失败（{E_IMG_DISABLED}）：图片搜索已关闭。")
            return
        q = (query or "").strip()
        if not q:
            yield event.plain_result(f"搜图失败（{E_EMPTY_QUERY}）：关键词为空。")
            return
        n = _clamp(count, 1, 5, self._default_image_count()) if count else self._default_image_count()
        code, results = await self._do_images(q, n)
        if code:
            yield event.plain_result(f"搜图失败（{code}）。")
            return

        sent = 0
        failed = 0
        for item in results:
            try:
                await event.send(event.make_result().url_image(item["murl"]))
                sent += 1
            except Exception:  # noqa: BLE001 —— 单张失败继续尝试剩余图片
                failed += 1
                logger.warning("[bing] %s", E_SEND_FAIL)
            await asyncio.sleep(0.3)  # 轻微节流，避免刷屏触发平台风控

        if sent == 0:
            yield event.plain_result(f"搜图失败（{E_SEND_FAIL}）：{failed} 张图片均发送失败。")
            return
        yield event.plain_result(f"图搜完成：已发送 {sent} 张，失败 {failed} 张（来源已随图片保留）。")

    # -- 生命周期 -----------------------------------------------------------

    async def terminate(self):
        """插件卸载/停用时释放网络会话。"""
        if self._client is not None:
            await self._client.close()
            self._client = None
