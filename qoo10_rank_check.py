"""
Qoo10 랭킹 모니터링 스크립트
- 지정한 카테고리 랭킹 페이지(기본: g=2, ビューティ)에서 1~50위 안에
  지정한 브랜드/셀러가 있는지 확인하고, 결과를 Discord 웹훅으로 전송한다.
- 결과 메시지는 탭(TAB)으로 구분된 표 형태라 Discord에서 복사해서
  엑셀에 그대로 붙여넣기 하면 열이 자동으로 나뉜다.
"""

import os
import re
import sys
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

# ── 설정값 (필요하면 이 부분만 수정) ──────────────────────────────
TARGET_URL = "https://www.qoo10.jp/gmkt.inc/Bestsellers/?g=2"  # ビューティ 카테고리 랭킹
TARGET_BRAND = "セラディックス"  # 추적할 브랜드/셀러명
TOP_N = 50  # 몇 위까지 확인할지
JST = timezone(timedelta(hours=9))
# ──────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.9",
}


def fetch_html(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=30)
    status = resp.status_code
    resp.raise_for_status()
    return resp.text, status


def looks_blocked(html: str) -> bool:
    if len(html) < 3000:
        return True
    lowered = html.lower()
    for kw in ["access denied", "captcha", "are you a robot", "blocked", "cloudflare"]:
        if kw in lowered:
            return True
    return False


def extract_price(text: str) -> str:
    """상품 블록 텍스트에서 '판매가'로 볼 수 있는 마지막 가격을 뽑아낸다."""
    prices = re.findall(r"[\d,]+円", text)
    if not prices:
        return ""
    non_mega_prices = []
    for m in re.finditer(r"([\d,]+円)(\s*メガポ時)?", text):
        if not m.group(2):
            non_mega_prices.append(m.group(1))
    if non_mega_prices:
        return non_mega_prices[-1]
    return prices[-1]


def parse_by_css(soup: BeautifulSoup, top_n: int):
    """1차 시도: rank 클래스를 가진 요소 기준으로 파싱."""
    items = []
    rank_nodes = soup.select(".rank")
    for rank_node in rank_nodes:
        rank_text = rank_node.get_text(strip=True)
        if not rank_text.isdigit():
            continue
        rank = int(rank_text)
        if rank < 1 or rank > top_n:
            continue

        container = rank_node
        for _ in range(6):
            if container.parent is None:
                break
            container = container.parent
            cls = container.get("class") or []
            if any("item" in c for c in cls):
                break

        block_text = container.get_text(separator=" ", strip=True)
        items.append({"rank": rank, "text": block_text})

    seen = set()
    unique_items = []
    for it in items:
        if it["rank"] in seen:
            continue
        seen.add(it["rank"])
        unique_items.append(it)
    unique_items.sort(key=lambda x: x["rank"])
    return unique_items


def parse_by_text_sequence(soup: BeautifulSoup, top_n: int):
    """2차 시도(폴백): 페이지 전체 텍스트에서 1,2,3...순서대로 등장하는
    순위 번호를 기준으로 상품 블록을 잘라낸다. (CSS 클래스에 의존하지 않음)"""
    text = soup.get_text("\n", strip=True)
    lines = [l for l in text.split("\n") if l]

    items = []
    expected = 1
    current_rank = None
    current_block = []

    def is_rank_marker(idx):
        if lines[idx] != str(expected):
            return False
        if idx == 0:
            return True
        prev = lines[idx - 1]
        return ("円" in prev) or ("ランキング" in prev) or ("メガポ時" in prev)

    i = 0
    while i < len(lines) and expected <= top_n:
        if is_rank_marker(i):
            if current_rank is not None:
                items.append({"rank": current_rank, "text": " ".join(current_block)})
            current_rank = expected
            current_block = []
            expected += 1
        elif current_rank is not None:
            current_block.append(lines[i])
        i += 1

    if current_rank is not None:
        items.append({"rank": current_rank, "text": " ".join(current_block)})

    return items


def parse_ranking(html: str, top_n: int):
    soup = BeautifulSoup(html, "html.parser")
    items = parse_by_css(soup, top_n)
    method = "css"
    if not items:
        items = parse_by_text_sequence(soup, top_n)
        method = "text-sequence"
    return items, method


def build_message(matches, checked_at, url, brand, top_n, diagnostics=None):
    header = f"Qoo10 랭킹 체크 결과 ({checked_at} JST)\nURL: {url}\n추적 브랜드: {brand}\n"

    table_header = "확인시각\t브랜드\t순위\t상품정보\t판매가"
    rows = [table_header]

    if not matches:
        rows.append(f"{checked_at}\t{brand}\t{top_n}위 밖\t-\t-")
        summary = f"결과: {top_n}위 안에 없음"
    else:
        for m in matches:
            price = extract_price(m["text"])
            title = m["text"]
            if len(title) > 60:
                title = title[:60] + "..."
            rows.append(f"{checked_at}\t{brand}\t{m['rank']}위\t{title}\t{price}")
        ranks = ", ".join(f"{m['rank']}위" for m in matches)
        summary = f"결과: {ranks} 진입"

    table = "\n".join(rows)
    msg = f"{header}{summary}\n\n```\n{table}\n```"

    if diagnostics:
        msg += f"\n(진단: {diagnostics})"

    return msg


def send_discord(webhook_url: str, content: str):
    if len(content) > 1900:
        content = content[:1900] + "\n... (생략)\n```"
    resp = requests.post(webhook_url, json={"content": content}, timeout=30)
    resp.raise_for_status()


def main():
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("ERROR: DISCORD_WEBHOOK_URL 환경변수가 설정되지 않았습니다.", file=sys.stderr)
        sys.exit(1)

    checked_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M")

    try:
        html, status = fetch_html(TARGET_URL)
        blocked = looks_blocked(html)
        items, method = parse_ranking(html, TOP_N)
        matches = [it for it in items if TARGET_BRAND in it["text"]]

        diagnostics = None
        # 파싱된 상품이 하나도 없거나(=사실상 실패), 차단 의심일 때만 진단 정보 첨부
        if not items or blocked:
            snippet = html[:400].replace("\n", " ").replace("`", "'")
            diagnostics = (
                f"HTTP상태={status}, HTML길이={len(html)}, 파싱방식={method}, "
                f"파싱된상품수={len(items)}, 차단의심={blocked}\n"
                f"응답미리보기: {snippet}"
            )

        message = build_message(matches, checked_at, TARGET_URL, TARGET_BRAND, TOP_N, diagnostics)
    except Exception as e:
        message = (
            f"Qoo10 랭킹 체크 중 오류 발생 ({checked_at} JST)\n"
            f"URL: {TARGET_URL}\n"
            f"오류: {type(e).__name__}: {e}"
        )

    send_discord(webhook_url, message)
    print("Done.")


if __name__ == "__main__":
    main()
