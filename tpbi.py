import os
import re
import sys
import time
from collections import deque
from urllib.parse import urljoin, urlparse, urldefrag, unquote

import requests
from bs4 import BeautifulSoup


HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

ASSET_EXTS = {
    ".css", ".js", ".mjs",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp4", ".webm", ".mp3", ".wav", ".ogg", ".m4a",
    ".pdf", ".zip", ".rar", ".7z", ".txt", ".xml", ".json"
}


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return urldefrag(url)[0]


def clean_name(name: str) -> str:
    name = unquote(name)
    name = re.sub(r'[\\:*?"<>|]', "_", name)
    name = name.replace("\r", "").replace("\n", "").strip()
    return name or "file"


def get_domain_variants(host: str):
    host = host.lower()
    variants = {host}
    if host.startswith("www."):
        variants.add(host[4:])
    else:
        variants.add("www." + host)
    return variants


def same_domain(url: str, allowed_domains: set[str]) -> bool:
    return urlparse(url).netloc.lower() in allowed_domains


def is_probably_html(resp: requests.Response) -> bool:
    ctype = (resp.headers.get("Content-Type") or "").lower()
    return "text/html" in ctype or "application/xhtml+xml" in ctype


def ext_of_url(url: str) -> str:
    path = urlparse(url).path.lower()
    _, ext = os.path.splitext(path)
    return ext


def get_filename_from_cd(resp: requests.Response):
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, flags=re.I)
    if m:
        return clean_name(m.group(1))
    return None


def build_local_path(base_dir: str, url: str, resp: requests.Response | None = None) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"

    if path.endswith("/"):
        folder = path.lstrip("/")
        filename = "index.html"
    else:
        folder = os.path.dirname(path).lstrip("/")
        filename = os.path.basename(path)

    if resp is not None:
        cd_name = get_filename_from_cd(resp)
        if cd_name:
            filename = cd_name

    filename = clean_name(filename)

    if not filename:
        filename = "index.html"

    full_folder = os.path.join(base_dir, folder.replace("/", os.sep))
    os.makedirs(full_folder, exist_ok=True)

    full_path = os.path.join(full_folder, filename)

    base, ext = os.path.splitext(full_path)
    n = 1
    while os.path.exists(full_path):
        full_path = f"{base}_{n}{ext}"
        n += 1

    return full_path


def save_bytes(path: str, content: bytes):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)


def parse_srcset(srcset: str):
    results = []
    for part in srcset.split(","):
        item = part.strip().split()[0] if part.strip() else ""
        if item:
            results.append(item)
    return results


def extract_links_and_assets(page_url: str, html: str):
    soup = BeautifulSoup(html, "html.parser")
    page_links = set()
    assets = set()

    for a in soup.find_all("a", href=True):
        u = urljoin(page_url, a["href"])
        u = urldefrag(u)[0]
        if u.startswith(("http://", "https://")):
            page_links.add(u)

    for tag in soup.find_all(["script", "img", "iframe", "source", "video", "audio"]):
        for attr in ["src"]:
            if tag.get(attr):
                u = urljoin(page_url, tag[attr])
                u = urldefrag(u)[0]
                if u.startswith(("http://", "https://")):
                    assets.add(u)

    for tag in soup.find_all(["link"]):
        if tag.get("href"):
            u = urljoin(page_url, tag["href"])
            u = urldefrag(u)[0]
            if u.startswith(("http://", "https://")):
                assets.add(u)

    for tag in soup.find_all(["img", "source"]):
        if tag.get("srcset"):
            for item in parse_srcset(tag["srcset"]):
                u = urljoin(page_url, item)
                u = urldefrag(u)[0]
                if u.startswith(("http://", "https://")):
                    assets.add(u)

    for tag in soup.find_all(style=True):
        css = tag.get("style", "")
        for m in re.findall(r'url\((.*?)\)', css, flags=re.I):
            raw = m.strip().strip('\'"')
            if raw and not raw.startswith("data:"):
                u = urljoin(page_url, raw)
                u = urldefrag(u)[0]
                if u.startswith(("http://", "https://")):
                    assets.add(u)

    return page_links, assets


def extract_css_urls(css_url: str, css_text: str):
    assets = set()
    for m in re.findall(r'url\((.*?)\)', css_text, flags=re.I):
        raw = m.strip().strip('\'"')
        if raw and not raw.startswith("data:"):
            u = urljoin(css_url, raw)
            u = urldefrag(u)[0]
            if u.startswith(("http://", "https://")):
                assets.add(u)
    return assets


def main():
    if len(sys.argv) < 2:
        print("ใช้: python tbpi.py www.example.com")
        return

    start_url = normalize_url(sys.argv[1])
    start_host = urlparse(start_url).netloc.lower()
    allowed_domains = get_domain_variants(start_host)

    out_dir = f"dump_{start_host.replace(':', '_')}"
    os.makedirs(out_dir, exist_ok=True)

    session = requests.Session()
    session.headers.update(HEADERS)

    queue = deque([start_url])
    seen_pages = set()
    seen_assets = set()
    downloaded_urls = set()

    max_pages = 10000
    delay = 0.2

    while queue and len(seen_pages) < max_pages:
        page_url = queue.popleft()
        page_url = normalize_url(page_url)

        if page_url in seen_pages:
            continue
        if not same_domain(page_url, allowed_domains):
            continue

        seen_pages.add(page_url)

        try:
            resp = session.get(page_url, timeout=25, allow_redirects=True)
            resp.raise_for_status()
        except Exception as e:
            print(f"[PAGE ERR] {page_url} -> {e}")
            continue

        final_url = normalize_url(resp.url)
        if not same_domain(final_url, allowed_domains):
            print(f"[SKIP OUTSIDE] {final_url}")
            continue

        downloaded_urls.add(final_url)

        if is_probably_html(resp):
            save_path = build_local_path(out_dir, final_url, resp)
            if save_path.lower().endswith((".js", ".css", ".png", ".jpg", ".jpeg", ".svg", ".woff", ".woff2")):
                save_path += ".html"
            save_bytes(save_path, resp.content)
            print(f"[HTML] {final_url} -> {save_path}")

            page_links, assets = extract_links_and_assets(final_url, resp.text)

            for link in sorted(page_links):
                if same_domain(link, allowed_domains) and link not in seen_pages:
                    queue.append(link)

            for asset in sorted(assets):
                if same_domain(asset, allowed_domains):
                    seen_assets.add(asset)
        else:
            save_path = build_local_path(out_dir, final_url, resp)
            save_bytes(save_path, resp.content)
            print(f"[FILE] {final_url} -> {save_path}")

        time.sleep(delay)

    asset_queue = deque(sorted(seen_assets))

    while asset_queue:
        asset_url = normalize_url(asset_queue.popleft())
        if asset_url in downloaded_urls:
            continue
        if not same_domain(asset_url, allowed_domains):
            continue

        try:
            resp = session.get(asset_url, timeout=25, allow_redirects=True)
            resp.raise_for_status()
        except Exception as e:
            print(f"[ASSET ERR] {asset_url} -> {e}")
            continue

        final_url = normalize_url(resp.url)
        if final_url in downloaded_urls or not same_domain(final_url, allowed_domains):
            continue

        downloaded_urls.add(final_url)

        try:
            save_path = build_local_path(out_dir, final_url, resp)
            save_bytes(save_path, resp.content)
            print(f"[ASSET] {final_url} -> {save_path}")
        except Exception as e:
            print(f"[SAVE ERR] {final_url} -> {e}")
            continue

        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "text/css" in ctype or ext_of_url(final_url) == ".css":
            try:
                more_assets = extract_css_urls(final_url, resp.text)
                for u in sorted(more_assets):
                    if same_domain(u, allowed_domains) and u not in downloaded_urls:
                        asset_queue.append(u)
            except Exception:
                pass

        time.sleep(delay)

    print("\nเสร็จแล้ว")
    print(f"บันทึกไว้ที่: {out_dir}")
    print(f"จำนวนหน้าที่สแกน: {len(seen_pages)}")
    print(f"จำนวน URL ที่ดาวน์โหลด: {len(downloaded_urls)}")


if __name__ == "__main__":
    main()
