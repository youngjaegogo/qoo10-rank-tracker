"""
Qoo10 랭킹 모니터링 스크립트 (Playwright / headless Chromium 기반)
- 일반 requests로는 Qoo10의 WAF가 봇으로 감지해 오류 페이지(523)를 돌려주기 때문에,
  실제 브라우저 엔진(headless Chromium)으로 접속해서 렌더링된 페이지에서 데이터를 읽는다.
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
from playwright.sync_api import sync_playwright

# ── 설정값 (필요하면 이 부분만 수정) ──────────────────────────────
TARGET_URL = "https://www.qoo10.jp/gmkt.inc/Bestsellers/?g=2"  # ビューティ 카테고리 랭킹
TARGET_BRAND = "セラディックス"  # 추적할 브랜드/셀러명
TOP_N = 100  # 몇 위까지 확인할지
JST = timezone(timedelta(hours=9))
# ──────────────────────────────────────────────────────────────

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def fetch_items_via_browser(url: str, top_n: int):
    """headless Chromium으로 실제 페이지를 렌더링해서 순위 아이템을 읽어온다."""
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(user_agent=DESKTOP_UA, locale="ja-JP")
        # networkidle은 광고/트래킹 스크립트가 계속 통신을 해서 끝까지 기다리면
        # 타임아웃이 나므로, DOM만 로드되면 바로 진행하고 랭킹 요소가 뜰 때까지만 기다린다.
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector("span.rank", timeout=30000)
        except Exception:
            pass
        page.wait_for_timeout(2000)

        raw_items = page.evaluate(
            """
            () => {
              const els = Array.from(document.querySelectorAll('.item'))
                .filter(el => el.querySelector('span.rank'));
              return els.map(el => {
                const rank = el.querySelector('span.rank').textContent.trim();
                const text = el.textContent.replace(/\\s+/g, ' ').trim();
                return { rank, text };
              });
            }
            """
        )

        page_title = page.title()
        browser.close()

    items = []
    for d in raw_items:
        if not d["rank"].isdigit():
            continue
        rank = int(d["rank"])
        if 1 <= rank <= top_n:
            items.append({"rank": rank, "text": d["text"]})

    seen = set()
    unique_items = []
    for it in sorted(items, key=lambda x: x["rank"]):
        if it["rank"] in seen:
            continue
        seen.add(it["rank"])
        unique_items.append(it)

    return unique_items, page_title


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
        items, page_title = fetch_items_via_browser(TARGET_URL, TOP_N)
        matches = [it for it in items if TARGET_BRAND in it["text"]]

        diagnostics = None
        if not items:
            diagnostics = f"파싱된상품수=0, 페이지제목={page_title}"

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
