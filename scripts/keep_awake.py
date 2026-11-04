"""Streamlit Cloud 앱이 잠들지 않게 몇 시간마다 들어가 본다 (GitHub Actions에서 실행).
잠들어 있으면 'Yes, get this app back up!' 버튼을 눌러 깨운다."""
import re
import sys

from playwright.sync_api import sync_playwright

URL = "https://snutranslate-l2tkvysuh6y5jmmcyr8nhu.streamlit.app/"

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    errors = []
    page.on("console", lambda m: m.type == "error" and errors.append(m.text[:200]))
    page.on("requestfailed", lambda r: errors.append("요청 실패 " + r.url[-80:]))
    page.on("response", lambda r: r.status >= 400 and errors.append(f"{r.status} {r.url[-80:]}"))
    page.goto(URL, timeout=120_000)
    page.wait_for_timeout(15_000)
    wake = page.get_by_role("button", name=re.compile(r"get this app back up", re.I))
    if wake.count():
        print("잠들어 있음 → 깨우기 버튼을 누름")
        wake.first.click()
        page.wait_for_timeout(90_000)
    else:
        print("깨어 있음")
    # 앱 화면(iframe 안의 '논문 번역기' 제목 또는 비밀번호 칸)이 뜰 때까지 최대 2분 기다린다
    ok = False
    for _ in range(24):
        for frame in page.frames:
            try:
                body = frame.inner_text("body", timeout=2_000)
            except Exception:
                continue
            if "논문 번역기" in body or "비밀번호" in body:
                ok = True
                break
        if ok:
            break
        page.wait_for_timeout(5_000)
    if not ok:
        print("frames:", [f.url for f in page.frames])
        print("오류:", errors[:10])
        page.screenshot(path="keep_awake.png", full_page=True)
        try:
            print("본문 앞부분:", page.inner_text("body", timeout=2_000)[:300])
        except Exception as e:
            print("본문 읽기 실패:", e)
    print("앱 화면 확인:", "됨" if ok else "안 됨")
    browser.close()
    sys.exit(0 if ok else 1)
