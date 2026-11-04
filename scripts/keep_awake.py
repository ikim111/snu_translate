"""Streamlit Cloud 앱이 잠들지 않게 몇 시간마다 들어가 본다 (GitHub Actions에서 실행).
잠들어 있으면 'Yes, get this app back up!' 버튼을 눌러 깨운다."""
import re
import sys

from playwright.sync_api import sync_playwright

URL = "https://snutranslate-l2tkvysuh6y5jmmcyr8nhu.streamlit.app/"

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto(URL, timeout=120_000)
    page.wait_for_timeout(15_000)
    wake = page.get_by_role("button", name=re.compile(r"get this app back up", re.I))
    if wake.count():
        print("잠들어 있음 → 깨우기 버튼을 누름")
        wake.first.click()
        page.wait_for_timeout(90_000)
    else:
        print("깨어 있음")
    # 앱 화면(iframe 안의 '논문 번역기' 제목)이 보이는지 확인
    ok = False
    for frame in page.frames:
        try:
            if frame.get_by_text("논문 번역기").count():
                ok = True
                break
        except Exception:
            pass
    print("앱 화면 확인:", "됨" if ok else "안 됨")
    browser.close()
    sys.exit(0 if ok else 1)
