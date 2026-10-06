"""
engines.py — 번역 엔진 (DeepL / OpenAI)

app.py는 엔진 종류를 몰라도 되게, 두 엔진 모두 같은 모양으로 만든다.

    engine = make_deepl(...)  또는  make_openai(...)
    engine.check()            → 키 확인, 안내 문구(남은 한도 등) 반환. 실패하면 EngineError
    engine.translate(texts)   → 같은 개수·같은 순서의 번역 리스트. 실패하면 EngineError
    engine.workers            → 동시에 번역할 페이지 수

번역할 조각은 HTML 조각이다. <i>(원문 이탤릭), <b>(run-in 소제목), <sup>(각주 번호),
<span translate="no">(URL·DOI·이메일)는 번역 후에도 그대로 남아 있어야 한다.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable

# ─────────────────────────── 공통 ───────────────────────────
BATCH_CHARS = 20_000      # 요청 1회에 보낼 최대 글자 수
TAG_RE = re.compile(r"<(i|b|u|sup)>|translate=\"no\"")

# OpenAI 모델과 1M 토큰당 가격(USD, 입력/출력). 2026년 10월 공식 문서 기준 — 바뀌면 여기만 고치면 된다.
OPENAI_MODELS: dict[str, tuple[str, float, float]] = {
    "gpt-6-luna": ("저렴·빠름", 0.10, 0.50),
    "gpt-6.1-sol": ("고품질", 2.00, 10.00),
}
DEFAULT_OPENAI_MODEL = "gpt-6.1-sol"   # 앱을 열었을 때 기본으로 선택되는 모델
USD_KRW = 1400            # 비용 안내용 대략 환율


class EngineError(Exception):
    """사용자에게 보여 줄 오류. fatal=True면 남은 페이지 번역을 멈춘다(키 오류, 한도 초과 등)."""

    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


@dataclass
class Engine:
    name: str
    check: Callable[[], str]
    translate: Callable[[list[str]], list[str]]
    workers: int = 1


def _plain_len(html: str) -> int:
    return len(re.sub(r"<[^>]+>", "", html))


def _tag_counts(html: str) -> list[int]:
    found = TAG_RE.findall(html)
    return [found.count(t) for t in ("i", "b", "u", "sup")] + [html.count('translate="no"')]


def split_text_into_chunks(text: str, limit: int = BATCH_CHARS) -> list[str]:
    """한 조각이 너무 길 때만 나눈다: 문장 → (최후) 공백 위치 순서로.
    논문 한 문단이 2만 자를 넘는 일은 거의 없어서 대부분 그대로 1조각이다."""
    if len(text) <= limit:
        return [text]
    parts = re.split(r"(?<=[.?!])\s+", text)
    chunks: list[str] = []
    cur = ""
    for p in parts:
        while len(p) > limit:
            cut = p.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            chunks.append(p[:cut])
            p = p[cut:].lstrip()
        if len(cur) + len(p) + 1 > limit:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def _batched(texts: list[str], send: Callable[[list[str]], list[str]]) -> list[str]:
    """조각들을 BATCH_CHARS 단위 요청으로 묶어 보내고, 너무 긴 조각은 나눴다가 다시 합친다."""
    pieces: list[str] = []
    owner: list[int] = []
    for i, t in enumerate(texts):
        for p in split_text_into_chunks(t):
            pieces.append(p)
            owner.append(i)
    results: list[str] = []
    batch: list[str] = []
    size = 0
    for p in pieces:
        if batch and size + len(p) > BATCH_CHARS:
            results += send(batch)
            batch, size = [], 0
        batch.append(p)
        size += len(p)
    if batch:
        results += send(batch)
    merged = [""] * len(texts)
    for i, r in zip(owner, results):
        merged[i] = f"{merged[i]} {r}".strip()
    return merged


# ─────────────────────────── DeepL ───────────────────────────
def make_deepl(api_key: str, target: str, glossary_entries: dict[str, str],
               glossary_cache: dict[str, Any]) -> Engine:
    """glossary_cache: 같은 용어집을 매번 새로 만들지 않도록 st.session_state 쪽 dict를 넘긴다."""
    import deepl

    # 키는 코드에 저장하지 않는다. Free/Pro 서버 선택은 SDK가 키 형식(':fx')으로 처리.
    translator = deepl.Translator(api_key.strip())
    state: dict[str, Any] = {"glossary": None, "notes": []}

    def wrap(e: Exception) -> EngineError:
        if isinstance(e, deepl.AuthorizationException):
            return EngineError("API Key 오류: DeepL 키가 올바르지 않습니다.", fatal=True)
        if isinstance(e, deepl.QuotaExceededException):
            return EngineError("DeepL 사용량 초과: 남은 글자 수가 없습니다.", fatal=True)
        if isinstance(e, deepl.TooManyRequestsException):
            return EngineError("요청이 너무 많음 — 잠시 후 다시 시도하세요.")
        if isinstance(e, deepl.ConnectionException):
            return EngineError(f"네트워크 오류: DeepL 서버에 연결할 수 없습니다. ({e})")
        return EngineError(f"API 요청 실패: {e}")

    def check() -> str:
        try:
            usage = translator.get_usage()
        except deepl.DeepLException as e:
            raise wrap(e)
        msg = ""
        if usage.character.valid:
            left = usage.character.limit - usage.character.count
            msg = f"DeepL 남은 글자 수: {left:,}자"
        if glossary_entries:
            sig = hashlib.md5(json.dumps(glossary_entries, sort_keys=True).encode()).hexdigest()[:10]
            if sig in glossary_cache:
                state["glossary"] = glossary_cache[sig]
            else:
                try:
                    g = translator.create_glossary(f"snu_translate_{sig}", source_lang="EN",
                                                   target_lang="KO", entries=glossary_entries)
                except deepl.DeepLException as e:
                    g = None
                    msg += f" · 용어집을 만들지 못해 용어집 없이 번역합니다({e})"
                glossary_cache[sig] = g
                state["glossary"] = g
        return msg

    def send(chunk: list[str]) -> list[str]:
        kw: dict[str, Any] = dict(target_lang=target, tag_handling="html")
        if state["glossary"] is not None:
            kw.update(source_lang="EN", glossary=state["glossary"])
        try:
            try:
                res = translator.translate_text(chunk, model_type="prefer_quality_optimized", **kw)
            except deepl.DeepLException as e:
                if "model_type" not in str(e):
                    raise
                res = translator.translate_text(chunk, **kw)
        except deepl.DeepLException as e:
            raise wrap(e)
        return [r.text for r in (res if isinstance(res, list) else [res])]

    return Engine("DeepL", check, lambda texts: _batched(texts, send), workers=1)


# ─────────────────────────── OpenAI ───────────────────────────
LANG_NAME = {"KO": "Korean", "EN-US": "English (US)"}

SYSTEM_PROMPT = """You are a professional translator of academic papers in education and statistics.
Translate each segment of the JSON array into {lang}. Return JSON {{"translations": [...]}} with exactly
the same number of items, in the same order, one translation per input segment.

Rules — follow all of them strictly:
1. Translate EVERY sentence completely. Never summarize, shorten, merge, or skip anything,
   including direct quotations, examples, and parenthetical remarks.
2. Keep the HTML tags <i>, </i>, <b>, </b>, <u>, </u>, <sup>, </sup> and wrap the corresponding translated
   words with them. Keep every <span translate="no">...</span> exactly as it is, untranslated.
3. Keep author names in their original spelling, years, statistics such as p < .05,
   F(2, 318) = 4.52, M = 3.24, SD = 0.81, numbers, URLs and DOIs exactly as in the source.
4. Citations: a citation inside parentheses stays exactly as in the source,
   e.g. (Mokros & Russell, 1995; Beaton et al., 1996). A citation that is part of the sentence
   is written the way Korean academic papers do it — translate "and", possessive "'s" and "et al.":
   "Biggs and Collis (1991) proposed" → "Biggs와 Collis(1991)는 … 제안하였다",
   "Carr and Begg's (1994) study" → "Carr와 Begg(1994)의 연구",
   "Jones et al. (2000) developed" → "Jones 등(2000)은 … 개발하였다",
   "Shaughnessy, Garfield, and Greer (1996)" → "Shaughnessy, Garfield, Greer(1996)".
5. <sup>n</sup> is a footnote number. Put it directly after the translated word or phrase that it
   follows in the source (e.g. "통계적 사고<sup>1</sup>"), never after an unrelated comma.
6. Use a formal academic written style (for Korean: 평서체 "~이다/~한다").
7. Use consistent terminology across segments. For statistics education terms prefer the Korean
   school-curriculum terms (data → 자료, measures of center → 대푯값,
   measures of spread/dispersion → 산포도, box-and-whisker plot → 상자그림).{glossary}
8. A segment may be a heading, a list item, a figure/table caption, a footnote, a short note such as
   an interview transcript line ("I:" interviewer, "S:" student), or a reference entry;
   translate it as that kind of text."""

# 지시문이 바뀌면 이전 번역 캐시를 쓰지 않도록 버전을 둔다
PROMPT_VERSION = "2026-10-06b"


def make_openai(api_key: str, model: str, target: str, glossary_entries: dict[str, str]) -> Engine:
    import openai

    client = openai.OpenAI(api_key=api_key.strip(), max_retries=3, timeout=180)
    gloss = ""
    if glossary_entries:
        gloss = "\n   Always use this glossary (English → translation):\n" + "\n".join(
            f"   - {en} → {ko}" for en, ko in glossary_entries.items())
    instructions = SYSTEM_PROMPT.format(lang=LANG_NAME.get(target, target), glossary=gloss)
    schema = {
        "type": "object",
        "properties": {"translations": {"type": "array", "items": {"type": "string"}}},
        "required": ["translations"],
        "additionalProperties": False,
    }

    def wrap(e: Exception) -> EngineError:
        if isinstance(e, openai.AuthenticationError):
            return EngineError("API Key 오류: OpenAI 키가 올바르지 않습니다.", fatal=True)
        if isinstance(e, openai.PermissionDeniedError):
            return EngineError(f"권한 오류: 이 키로 '{model}' 모델을 쓸 수 없습니다.", fatal=True)
        if isinstance(e, openai.NotFoundError):
            return EngineError(f"모델 이름 오류: '{model}' 모델을 찾을 수 없습니다.", fatal=True)
        if isinstance(e, openai.RateLimitError):
            if "insufficient_quota" in str(e):
                return EngineError("OpenAI 크레딧 부족: platform.openai.com에서 충전하세요.", fatal=True)
            return EngineError("요청이 너무 많음 — 잠시 후 다시 시도하세요.")
        if isinstance(e, (openai.APIConnectionError, openai.APITimeoutError)):
            return EngineError(f"네트워크 오류: OpenAI 서버에 연결할 수 없습니다. ({e})")
        return EngineError(f"API 요청 실패: {e}")

    def check() -> str:
        try:
            client.models.retrieve(model)          # 키와 모델 이름을 한 번에 확인 (비용 없음)
        except openai.OpenAIError as e:
            raise wrap(e)
        return f"OpenAI 모델: {model}"

    def call(chunk: list[str], extra: str = "") -> list[str]:
        kw: dict[str, Any] = dict(
            model=model,
            instructions=instructions + extra,
            input=json.dumps({"segments": chunk}, ensure_ascii=False),
            text={"format": {"type": "json_schema", "name": "translations", "schema": schema, "strict": True}},
        )
        try:
            try:
                resp = client.responses.create(reasoning={"effort": "low"}, **kw)
            except openai.BadRequestError as e:
                if "reasoning" not in str(e):
                    raise
                resp = client.responses.create(**kw)   # reasoning 옵션이 없는 모델
            out = json.loads(resp.output_text)["translations"]
        except openai.OpenAIError as e:
            raise wrap(e)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            raise EngineError(f"번역 결과 형식 오류: {e}")
        if len(out) != len(chunk):
            raise EngineError(f"번역 결과 개수가 맞지 않습니다 ({len(chunk)}개 → {len(out)}개).")
        return out

    def suspicious(src: str, tr: str) -> bool:
        """누락(너무 짧음)이나 태그 유실이 의심되는지."""
        n = _plain_len(src)
        if target == "KO" and n > 150 and _plain_len(tr) < n * 0.3:
            return True
        return _tag_counts(src) != _tag_counts(tr)

    def send(chunk: list[str]) -> list[str]:
        out = call(chunk)
        # LLM은 가끔 문장을 빼먹거나 태그를 잃는다 → 의심스러운 조각만 한 번 더 번역
        for i, (s, t) in enumerate(zip(chunk, out)):
            if suspicious(s, t):
                try:
                    again = call([s], "\n\nIMPORTANT: The previous attempt omitted content or tags. "
                                      "Translate the whole segment sentence by sentence and keep all tags.")[0]
                    if not suspicious(s, again) or _plain_len(again) > _plain_len(t):
                        out[i] = again
                except EngineError:
                    pass
        return out

    return Engine(f"OpenAI ({model})", check, lambda texts: _batched(texts, send), workers=4)


def openai_cost_krw(model: str, src_chars: int) -> int | None:
    """영어 원문 글자 수로 OpenAI 비용(원)을 대략 추정. 지시문 반복 비용 포함 거친 값."""
    if model not in OPENAI_MODELS:
        return None
    _, pin, pout = OPENAI_MODELS[model]
    tokens_in = src_chars / 4 * 1.3          # 원문 + 페이지마다 반복되는 지시문
    tokens_out = src_chars * 0.4             # 한국어 번역문 (대략)
    usd = tokens_in / 1e6 * pin + tokens_out / 1e6 * pout
    return round(usd * USD_KRW)
