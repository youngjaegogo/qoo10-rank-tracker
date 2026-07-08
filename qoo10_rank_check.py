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


def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def extract_price(text: str) -> str:
    """상품 블록 텍스트에서 '판매가'로 볼 수 있는 마지막 가격을 뽑아낸다."""
    prices = re.findall(r"[\d,]+円", text)
    if not prices:
        return ""
    # "메가포 시" 할인가는 제외하고, 그 앞의 가격(정상 판매가)을 우선 사용
    non_mega_prices = []
    for m in re.finditer(r"([\d,]+円)(\s*メガポ時)?", text):
        if not m.group(2):
            non_mega_prices.append(m.group(1))
    if non_mega_prices:
        return non_mega_prices[-1]
    return prices[-1]


def parse_ranking(html: str, top_n: int):
    """랭킹 페이지 HTML을 파싱해서 [{rank, title, text}, ...] 형태로 반환."""
    soup = BeautifulSoup(html, "html.parser")
    items = []

    rank_nodes = soup.select(".rank")
    for rank_node in rank_nodes:
        rank_text = rank_node.get_text(strip=True)
        if not rank_text.isdigit():
            continue
        rank = int(rank_text)
        if rank < 1 or rank > top_n:
            continue

        # rank 배지를 감싸는 상품 컨테이너 찾기 (item 클래스가 붙은 조상 요소)
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

    # rank 기준 중복 제거 (같은 rank가 여러 번 잡히는 경우 첫 번째만 사용)
    seen = set()
    unique_items = []
    for it in items:
        if it["rank"] in seen:
            continue
        seen.add(it["rank"])
        unique_items.append(it)

    unique_items.sort(key=lambda x: x["rank"])
    return unique_items


def build_message(matches, checked_at: str, url: str, brand: str, top_n: int) -> str:
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
            # 너무 길면 앞부분만
            if len(title) > 60:
                title = title[:60] + "..."
            rows.append(f"{checked_at}\t{brand}\t{m['rank']}위\t{title}\t{price}")
        ranks = ", ".join(f"{m['rank']}위" for m in matches)
        summary = f"결과: {ranks} 진입"

    table = "\n".join(rows)
    return f"{header}{summary}\n\n```\n{table}\n```"


def send_discord(webhook_url: str, content: str):
    # Discord 메시지 길이 제한(2000자) 대비 잘라내기
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
        html = fetch_html(TARGET_URL)
        items = parse_ranking(html, TOP_N)
        matches = [it for it in items if TARGET_BRAND in it["text"]]
        message = build_message(matches, checked_at, TARGET_URL, TARGET_BRAND, TOP_N)
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
