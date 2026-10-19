
"""
UniBridge - Wenzao Campus News
Lấy danh sách thông báo và nội dung bài viết từ website Wenzao.
Yêu cầu: pip install httpx
"""

import logging
import os
import re
import threading
import time
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

logger = logging.getLogger(__name__)

SOURCES = [
    "https://a001.wzu.edu.tw/list/6618",
    "https://a001.wzu.edu.tw/category/154196",
]

REFRESH_SECONDS = 1800
MAX_ARTICLES = 12
MAX_ARTICLE_CHARS = 1800

NEWS_RULES = """
Bạn là trợ lý thông báo trường Wenzao.
Chỉ trả lời dựa trên tiêu đề, ngày đăng, nội dung và đường dẫn
của thông báo được cung cấp.
Không tự đoán hạn nộp, điều kiện nhận học bổng hoặc thủ tục.
Nếu không tìm thấy thông tin, hãy nói rõ là chưa tìm thấy.
Khi trả lời, hãy kèm đường dẫn thông báo chính thức để người dùng kiểm tra.
"""

ARTICLE_RE = re.compile(r"/article/\d+")
DATE_RE = re.compile(r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?")


class ListingParser(HTMLParser):
    """Trích xuất tiêu đề và liên kết bài thông báo."""

    def __init__(self, page_url):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.items = []
        self.href = None
        self.text = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        if tag in ("script", "style", "noscript"):
            self.skip += 1
            return

        if tag == "a":
            href = attrs.get("href", "")
            if ARTICLE_RE.search(href):
                self.href = urljoin(self.page_url, href)
                self.text = []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1
            return

        if tag == "a" and self.href:
            title = " ".join("".join(self.text).split())

            if title and len(title) >= 4:
                self.items.append({
                    "title": title,
                    "url": self.href,
                    "date": "",
                    "content": "",
                    "source": self.page_url,
                })

            self.href = None
            self.text = []

    def handle_data(self, data):
        if self.skip:
            return

        if self.href:
            self.text.append(data)


class ArticleParser(HTMLParser):
    """Trích xuất nội dung văn bản sau tiêu đề bài viết."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.parts = []
        self.in_h1 = False
        self.found_h1 = False
        self.skip = 0
        self.stopped = False

    def handle_starttag(self, tag, attrs):
        if self.stopped:
            return

        if tag in ("script", "style", "noscript", "svg"):
            self.skip += 1
            return

        if tag == "h1":
            self.in_h1 = True
            self.found_h1 = True
        elif self.found_h1 and not self.skip:
            if tag in (
                "p", "div", "section", "article", "main",
                "li", "br", "h2", "h3", "h4", "tr"
            ):
                self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self.skip:
            self.skip -= 1
            return

        if tag == "h1":
            self.in_h1 = False
        elif self.found_h1 and not self.skip:
            if tag in (
                "p", "div", "section", "article", "main",
                "li", "br", "h2", "h3", "h4", "tr"
            ):
                self.parts.append("\n")

    def handle_data(self, data):
        if self.stopped or self.skip:
            return

        text = " ".join(data.split())
        if not text:
            return

        # Stop before the website footer.
        if (
            "80793 高雄市三民區" in text
            or "©Copyright" in text
            or "版權所有" in text
        ):
            self.stopped = True
            return

        if self.in_h1:
            self.title += (" " if self.title else "") + text
        elif self.found_h1:
            self.parts.append(text)


def _fetch(client, url):
    response = client.get(url)
    response.raise_for_status()
    return response.text


def _parse_listing(html, page_url):
    parser = ListingParser(page_url)
    parser.feed(html)
    return parser.items


def _parse_article(html):
    parser = ArticleParser()
    parser.feed(html)

    lines = []
    seen = set()

    for line in "\n".join(parser.parts).splitlines():
        line = " ".join(line.split())

        if not line or line == parser.title or line in seen:
            continue

        if line in ("分享", "列印", "回上一頁", "上一篇", "下一篇"):
            continue

        seen.add(line)
        lines.append(line)

    content = "\n".join(lines).strip()
    return parser.title.strip(), content


_cache = []
_cache_lock = threading.Lock()
_refresh_lock = threading.Lock()
_started = False


def refresh():
    """更新列表并读取最新公告正文。"""

    collected = []
    headers = {"User-Agent": "UniBridge-WZU-NewsBot/1.0"}

    with httpx.Client(
        timeout=18,
        follow_redirects=True,
        headers=headers,
    ) as client:

        # 1. 获取公告列表
        for source in SOURCES:
            try:
                html = _fetch(client, source)
                collected.extend(_parse_listing(html, source))
            except Exception:
                logger.exception("读取公告列表失败：%s", source)

        # 2. 去除重复链接
        unique = []
        seen_urls = set()

        for item in collected:
            url = item["url"].split("#", 1)[0]

            if url in seen_urls:
                continue

            seen_urls.add(url)
            item["url"] = url
            unique.append(item)

        # 3. 获取最新公告正文
        for item in unique[:MAX_ARTICLES]:
            try:
                html = _fetch(client, item["url"])
                title, content = _parse_article(html)

                if title:
                    item["title"] = title

                item["content"] = content[:MAX_ARTICLE_CHARS]

                match = DATE_RE.search(content)
                if match:
                    item["date"] = match.group(0)

            except Exception:
                logger.exception("读取公告正文失败：%s", item["url"])

    with _cache_lock:
        _cache[:] = unique
        result = [dict(item) for item in _cache]

    logger.info(
        "Wenzao 公告更新完成：共 %d 条，成功获取正文 %d 条",
        len(result),
        sum(bool(x["content"]) for x in result),
    )

    return result


def get_news_context(max_items=8, max_chars=6000):
    """提供给 AI 使用的公告上下文。"""

    with _cache_lock:
        items = [dict(item) for item in _cache]

    if not items:
        return "目前尚未获取到学校公告，请不要猜测公告内容。"

    blocks = []
    used = 0

    for index, item in enumerate(items[:max_items], 1):
        block = (
            f"公告 {index}\n"
            f"标题：{item['title']}\n"
            f"日期：{item.get('date') or '未能识别'}\n"
            f"官方链接：{item['url']}\n"
            f"正文：{item.get('content') or '未能获取正文，请查看官方链接。'}"
        )

        remaining = max_chars - used

        if remaining <= 0:
            break

        if len(block) > remaining:
            if remaining > 100:
                blocks.append(block[:remaining] + "……")
            break

        blocks.append(block)
        used += len(block) + 2

    return "\n\n".join(blocks)


def prompt_block(max_chars=6000):
    return (
        NEWS_RULES
        + "\n\n以下是目前获取到的学校公告：\n"
        + get_news_context(max_chars=max_chars)
    )


def _refresh_loop():
    while True:
        try:
            refresh()
        except Exception:
            logger.exception("公告后台更新失败")

        time.sleep(REFRESH_SECONDS)


def start_background_refresh():
    """启动后台定时更新线程。"""

    global _started

    with _refresh_lock:
        if _started:
            return

        _started = True

        thread = threading.Thread(
            target=_refresh_loop,
            name="wzu-news-refresh",
            daemon=True,
        )
        thread.start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("正在测试 Wenzao 公告抓取……")

    items = refresh()

    print(f"\n公告数量：{len(items)}")
    print(
        "成功获取正文：",
        sum(bool(item["content"]) for item in items),
    )
    print("\n公告预览：")
    print(get_news_context(max_items=3, max_chars=3500))
