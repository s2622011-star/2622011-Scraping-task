"""武蔵野大学公式サイトの公開HTMLを巡回して、URLとタイトルをCSVに出力する。"""

import argparse
import csv
import json
import sys
import time
from collections import deque
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup


START_URL = "https://www.musashino-u.ac.jp/"
HOST = urlsplit(START_URL).hostname
USER_AGENT = "UniversityClassSitemap/1.0"
SKIP_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv",
    ".zip", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif", ".ico",
    ".mp3", ".mp4", ".mov", ".css", ".js", ".xml",
}
TRACKING_KEYS = {"fbclid", "gclid", "yclid", "_ga"}


def page_url(base, href):
    """相対リンクを解決し、同じホスト内の巡回候補だけを返す。"""
    try:
        parsed = urlsplit(urljoin(base, href.strip()))
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname != HOST
            or parsed.username is not None
            or parsed.port not in {None, 443 if parsed.scheme == "https" else 80}
        ):
            return None
    except ValueError:
        return None
    path = parsed.path or "/"
    if Path(path.lower()).suffix in SKIP_EXTENSIONS:
        return None
    parameters = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key.lower() in TRACKING_KEYS or key.lower().startswith("utm_") for key, _ in parameters):
        parameters = [
            (key, value) for key, value in parameters
            if key.lower() not in TRACKING_KEYS and not key.lower().startswith("utm_")
        ]
        query = urlencode(parameters)
    else:
        query = parsed.query
    return urlunsplit((parsed.scheme, HOST, path, query, ""))


def get_page(session, url, delay):
    # リダイレクト先も候補に戻し、外部ホストへの接続や連続アクセスを防ぐ。
    time.sleep(delay)
    return session.get(url, timeout=(8, 20), allow_redirects=False, stream=True)


def load_robots(session):
    robots = RobotFileParser()
    with get_page(session, urljoin(START_URL, "robots.txt"), 1) as response:
        if response.status_code in {404, 410}:
            robots.parse([])
        elif response.status_code == 200:
            robots.parse(response.text.splitlines())
        else:
            raise RuntimeError(f"robots.txt を確認できません（HTTP {response.status_code}）")
    delay = max(1, robots.crawl_delay(USER_AGENT) or 0)
    rate = robots.request_rate(USER_AGENT)
    if rate and rate.requests:
        delay = max(delay, rate.seconds / rate.requests)
    return robots, delay


def save_state(path, pending, visited, titles, failures):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump({
            "pending": list(pending),
            "visited": sorted(visited),
            "titles": titles,
            "failures": failures,
        }, file, ensure_ascii=False)
    temporary.replace(path)


def write_csv(path, titles):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["url", "title"])
        writer.writerows(titles.items())
    temporary.replace(path)


def crawl(state_file, output, limit):
    if state_file.exists():
        state = json.loads(state_file.read_text(encoding="utf-8"))
    else:
        state = {"pending": [START_URL], "visited": [], "titles": {}, "failures": {}}
    pending = deque(state["pending"])
    visited = set(state["visited"])
    scheduled = visited | set(pending)
    titles = state["titles"]  # 課題で指定された {URL: <title> の文字列}
    failures = state["failures"]
    processed = 0

    def enqueue(base, href):
        target = page_url(base, href)
        if target and target not in scheduled:
            scheduled.add(target)
            pending.append(target)

    with requests.Session() as session:
        session.headers["User-Agent"] = USER_AGENT
        try:
            robots, delay = load_robots(session)
            while pending and (limit is None or processed < limit):
                url = pending[0]
                if not robots.can_fetch(USER_AGENT, url):
                    failures[url] = "robots.txt"
                else:
                    try:
                        with get_page(session, url, delay) as response:
                            if response.status_code in {429, 503}:
                                raise RuntimeError(f"HTTP {response.status_code} のため中断: {url}")
                            if response.is_redirect:
                                enqueue(url, response.headers["Location"])
                            elif response.status_code != 200:
                                failures[url] = f"HTTP {response.status_code}"
                            elif "html" in response.headers.get("Content-Type", "").lower():
                                soup = BeautifulSoup(response.content, "html.parser")
                                titles[url] = soup.title.get_text(" ", strip=True) if soup.title else ""
                                base = soup.find("base", href=True)
                                base_url = urljoin(url, base["href"]) if base else url
                                for link in soup.find_all("a", href=True):
                                    enqueue(base_url, link["href"])
                    except requests.RequestException as error:
                        failures[url] = type(error).__name__
                pending.popleft()
                visited.add(url)
                processed += 1
                if processed % 25 == 0:
                    save_state(state_file, pending, visited, titles, failures)
                    print(f"確認 {len(visited)} / HTML {len(titles)} / 残り {len(pending)} / 取得不可 {len(failures)}", flush=True)
        finally:
            save_state(state_file, pending, visited, titles, failures)

    if not pending and titles:
        write_csv(output, titles)
        print(f"{len(titles)} 件を {output} に保存。取得不可 {len(failures)} 件。", flush=True)
    else:
        print(f"途中経過: HTML {len(titles)} 件、残り {len(pending)} 件。次の実行で再開できます。", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("sitemap.csv"))
    parser.add_argument("--state", type=Path, default=Path(".crawl-state.json"))
    parser.add_argument("--limit", type=int, help="動作確認用。省略時は巡回候補がなくなるまで処理する")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit は1以上を指定してください")
    try:
        crawl(args.state, args.output, args.limit)
    except (RuntimeError, requests.RequestException, KeyboardInterrupt) as error:
        print(f"巡回を中断しました。{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
