"""
library.py — '내 서재': 번역한 논문을 GitHub 비공개 저장소에 보관

Streamlit Community Cloud의 디스크는 앱이 잠들거나 다시 배포되면 지워진다.
그래서 번역 결과를 사용자의 GitHub 비공개 저장소에 올려 두고, 언제든 목록을 보고 다시 받는다.

필요한 Secrets
    LIBRARY_REPO = "ikim111/snu_translate_library"   # 반드시 비공개(Private) 저장소
    GITHUB_TOKEN = "github_pat_..."                   # 이 저장소에만 Contents 읽기/쓰기 권한

저장소 안의 구조
    index.json                       논문 목록 (빠른 목록 표시용)
    papers/<paper_id>/meta.json      제목, 날짜, 엔진, 쪽수 등
    papers/<paper_id>/translation.pdf
    papers/<paper_id>/original.pdf   원문 (나중에 번역비 없이 PDF를 다시 만들 때 필요)
    papers/<paper_id>/structure.json.gz  원문 읽기 결과 (스캔본을 다시 읽지 않도록)
    papers/<paper_id>/progress.json  번역 데이터 (앱의 '진행 파일'과 같은 형식)
    papers/<paper_id>/ocr.json       스캔 쪽 글자 읽기(OCR) 결과
    glossary.json                    내 용어집 {영어: 한국어}

paper_id는 원문 PDF의 해시 앞 16자라서, 같은 논문은 항상 같은 자리에 덮어쓴다.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

API = "https://api.github.com"
KST = timezone(timedelta(hours=9))
TIMEOUT = 60


class LibraryError(Exception):
    pass


class Library:
    def __init__(self, repo: str, token: str):
        self.repo = repo.strip()
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token.strip()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    # ── 저수준: 파일 읽기/쓰기/지우기 ──
    def _url(self, path: str) -> str:
        return f"{API}/repos/{self.repo}/contents/{path}"

    def _check(self, r: requests.Response, what: str) -> None:
        if r.status_code in (401, 403):
            raise LibraryError(f"GitHub 권한 오류({what}): 토큰이 틀렸거나 이 저장소에 쓰기 권한이 없습니다.")
        if r.status_code == 404:
            raise LibraryError(f"GitHub에서 찾을 수 없음({what}): 저장소 이름을 확인하세요.")
        if r.status_code >= 400:
            raise LibraryError(f"GitHub 오류 {r.status_code} ({what}): {r.text[:200]}")

    def _sha(self, path: str) -> str | None:
        r = self.s.get(self._url(path), timeout=TIMEOUT)
        if r.status_code == 404:
            return None
        self._check(r, path)
        return r.json().get("sha")

    def read(self, path: str) -> bytes | None:
        """파일 내용(바이트). 없으면 None. raw 형식으로 받아 1MB 넘는 파일도 읽는다."""
        r = self.s.get(self._url(path), headers={"Accept": "application/vnd.github.raw+json"},
                       timeout=TIMEOUT)
        if r.status_code == 404:
            return None
        self._check(r, path)
        return r.content

    def write(self, path: str, data: bytes, message: str) -> None:
        body: dict[str, Any] = {"message": message, "content": base64.b64encode(data).decode()}
        sha = self._sha(path)
        if sha:
            body["sha"] = sha
        r = self.s.put(self._url(path), json=body, timeout=TIMEOUT)
        self._check(r, path)

    def remove(self, path: str, message: str) -> None:
        sha = self._sha(path)
        if not sha:
            return
        r = self.s.delete(self._url(path), json={"message": message, "sha": sha}, timeout=TIMEOUT)
        self._check(r, path)

    # ── 고수준: 논문 단위 ──
    def check(self) -> None:
        """저장소에 접근 가능한지, 비공개인지 확인."""
        r = self.s.get(f"{API}/repos/{self.repo}", timeout=TIMEOUT)
        self._check(r, self.repo)
        if not r.json().get("private", False):
            raise LibraryError(f"'{self.repo}'는 공개 저장소입니다. 논문 번역본은 비공개(Private) 저장소에만 저장하세요.")

    def list_papers(self) -> list[dict]:
        raw = self.read("index.json")
        if not raw:
            return []
        try:
            items = json.loads(raw.decode("utf-8"))
        except Exception:
            return []
        return sorted(items, key=lambda m: m.get("updated", ""), reverse=True)

    def _save_index(self, items: list[dict], message: str) -> None:
        self.write("index.json", json.dumps(items, ensure_ascii=False, indent=1).encode("utf-8"), message)

    def save_paper(self, meta: dict, files: dict[str, bytes]) -> dict:
        """논문 하나 저장(같은 paper_id면 덮어씀). files: {파일명: 바이트}"""
        pid = meta["id"]
        meta = {**meta, "updated": datetime.now(KST).strftime("%Y-%m-%d %H:%M")}
        title = meta.get("title", pid)[:60]
        for name, data in files.items():
            self.write(f"papers/{pid}/{name}", data, f"{title}: {name}")
        self.write(f"papers/{pid}/meta.json", json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8"),
                   f"{title}: meta")
        items = [m for m in self.list_papers() if m.get("id") != pid] + [meta]
        self._save_index(items, f"서재 목록 갱신: {title}")
        return meta

    def get_file(self, pid: str, name: str) -> bytes | None:
        return self.read(f"papers/{pid}/{name}")

    def delete_paper(self, pid: str) -> None:
        for name in ("translation.pdf", "original.pdf", "progress.json", "ocr.json", "structure.json.gz", "meta.json"):
            self.remove(f"papers/{pid}/{name}", f"삭제: {pid}/{name}")
        items = [m for m in self.list_papers() if m.get("id") != pid]
        self._save_index(items, f"서재 목록에서 삭제: {pid}")

    # ── 용어집 ──
    def get_glossary(self) -> dict[str, str] | None:
        raw = self.read("glossary.json")
        if raw is None:
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
            return {str(k): str(v) for k, v in data.items()}
        except Exception:
            return None

    def save_glossary(self, entries: dict[str, str]) -> None:
        ordered = dict(sorted(entries.items(), key=lambda kv: kv[0].lower()))
        self.write("glossary.json", json.dumps(ordered, ensure_ascii=False, indent=1).encode("utf-8"),
                   f"용어집 갱신 ({len(ordered)}개)")
