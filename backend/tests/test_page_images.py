"""网页配图提取：`og:image` 优先、相对地址要补全、图标要过滤。

纯函数测试，不发网络请求——真实抓取靠 ``scripts/try_page_images.py`` 手工验证。
"""

from __future__ import annotations

from app.services.ai.page_images import find_image_urls, looks_like_content_image


def test_og_image_comes_first():
    """站点自己挑的分享图（og:image）比页面里随便一张图更合适，要排在最前。"""
    html = """
    <html><head>
      <meta property="og:image" content="https://cdn.example.com/cover.png">
    </head><body>
      <img src="https://cdn.example.com/body1.png">
    </body></html>
    """
    urls = find_image_urls(html, "https://example.com/post")
    assert urls[0] == "https://cdn.example.com/cover.png"
    assert "https://cdn.example.com/body1.png" in urls


def test_relative_urls_are_resolved():
    """网页里大量相对路径，不补全就下载不了。"""
    html = '<img src="/static/a.png"><img src="images/b.png">'
    urls = find_image_urls(html, "https://example.com/dir/page.html")
    assert "https://example.com/static/a.png" in urls
    assert "https://example.com/dir/images/b.png" in urls


def test_data_uris_and_javascript_are_dropped():
    """data: URI 是内嵌图，不是可下载的地址；javascript: 更不该碰。"""
    html = (
        '<img src="data:image/png;base64,AAAA">'
        '<img src="javascript:void(0)">'
        '<img src="https://cdn.example.com/real.png">'
    )
    urls = find_image_urls(html, "https://example.com/")
    assert urls == ["https://cdn.example.com/real.png"]


def test_srcset_uses_the_first_candidate():
    """srcset 形如 'a.jpg 1x, b.jpg 2x'，取第一段即可。"""
    html = '<img srcset="https://cdn.example.com/a.jpg 1x, https://cdn.example.com/b.jpg 2x">'
    urls = find_image_urls(html, "https://example.com/")
    assert urls == ["https://cdn.example.com/a.jpg"]


def test_twitter_image_is_a_fallback_for_og_image():
    html = '<meta name="twitter:image" content="https://cdn.example.com/tw.png">'
    assert find_image_urls(html, "https://example.com/") == ["https://cdn.example.com/tw.png"]


def test_duplicates_are_removed_in_order():
    html = (
        '<meta property="og:image" content="https://cdn.example.com/x.png">'
        '<img src="https://cdn.example.com/x.png">'
        '<img src="https://cdn.example.com/y.png">'
    )
    assert find_image_urls(html, "https://example.com/") == [
        "https://cdn.example.com/x.png",
        "https://cdn.example.com/y.png",
    ]


def test_icons_and_avatars_are_filtered_out():
    """图标/头像发到群里没意义，实测 GitHub 页面上大量 shields 徽章。"""
    assert not looks_like_content_image("https://img.shields.io/pypi/v/requests.svg")
    assert not looks_like_content_image("https://example.com/logo.png")
    assert not looks_like_content_image("https://example.com/avatar/1.jpg")
    assert not looks_like_content_image("https://example.com/tracking-pixel.gif")
    # 正常的正文配图要保留。
    assert looks_like_content_image("https://cdn.example.com/screenshot-1.png")
    assert looks_like_content_image("https://opengraph.githubassets.com/abc/psf/requests")
