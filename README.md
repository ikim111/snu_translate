# 논문 정독용 PDF 번역기

영어 논문 PDF를 DeepL 또는 OpenAI로 번역해, **원문과 같은 판형의 번역 PDF**로 만들어 주는 Streamlit 앱입니다.

- 원문 1쪽 = 번역 1쪽 (넘치면 글자·줄간격을 자동으로 줄이고, 그래도 넘치면 "(계속)" 페이지)
- 각 페이지 오른쪽 위에 `PDF p.4 · 인쇄 25쪽` 표시 → 원문 찾기 쉬움
- 원문의 *이탤릭*은 번역문에서 **볼드** (통계 기호 *M*, *SD*, *p*와 참고문헌은 제외)
- 그림·표는 원문에서 잘라 그대로, 캡션은 번역
- 가로로 돌려 놓은 표 페이지도 칸 단위로 번역해 표로 다시 그림 (굵은 글씨·밑줄 유지). 구조를 못 읽으면 원문 그대로
- 페이지 경계에서 끊긴 문장은 시작한 페이지에서 완결
- 참고문헌은 기본적으로 원문 유지 (옵션)
- 용어집으로 학술 용어 번역을 통일 — 논문에서 핵심 용어를 **추천**받아 확인 후 확정, 서재에 저장되어 계속 쌓임
- 번역은 페이지마다 저장 → 중간에 멈춰도 이어서 번역, 진행 파일(.json) 내려받기/올리기 가능
- 📚 **내 서재**: 번역한 논문을 GitHub 비공개 저장소에 자동 보관, 목록·검색·다시 받기·삭제

## 파일 구성

| 파일 | 역할 |
|---|---|
| `app.py` | Streamlit 화면, 캐시, 다운로드 |
| `engines.py` | 번역 엔진: DeepL / OpenAI (모델·가격 표도 여기에) |
| `library.py` | 내 서재: GitHub 비공개 저장소에 번역본 보관 |
| `rotated_table.py` | 가로로 돌려 놓은 표 페이지의 칸 구조 읽기·표로 다시 그리기 |
| `pdf_core.py` | PDF 추출, 읽기 순서(2단 포함), 문단 복원, 그림·표·참고문헌, 번역 PDF 조판 |
| `fonts/` | 번역 PDF용 한글 글꼴 (나눔고딕, SIL OFL) |

## 내 컴퓨터에서 실행

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Streamlit Community Cloud 배포 (무료)

1. https://share.streamlit.io 에 GitHub 계정으로 로그인
2. **Create app** → 저장소 `ikim111/snu_translate`, 브랜치 `main`, 파일 `app.py`
3. (선택) **Advanced settings → Secrets**에 아래를 넣으면 키를 매번 입력하지 않아도 됩니다.

```toml
DEEPL_API_KEY = "DeepL-키"
OPENAI_API_KEY = "sk-..."
APP_PASSWORD = "나만-아는-비밀번호"
```

> API 키를 넣을 때는 **반드시 `APP_PASSWORD`도 함께** 넣으세요.
> 앱 주소를 아는 누구나 내 DeepL 한도를 쓰게 되는 것을 막습니다.

12시간 동안 아무도 접속하지 않으면 앱이 잠들고, 다시 접속하면 깨어납니다. 이때 서버의 번역 캐시가
사라질 수 있으니, 긴 논문은 화면의 **진행 파일 받기**로 저장해 두세요.

## 📚 내 서재 연결 (선택)

Streamlit 무료 서버의 저장 공간은 앱이 잠들거나 다시 배포되면 지워집니다.
번역본을 계속 보관하려면 **비공개** GitHub 저장소를 서재로 연결하세요.

1. GitHub에서 **Private** 저장소를 만듭니다 (예: `snu_translate_library`). 이 공개 저장소(`snu_translate`)에는
   논문 번역본을 넣지 마세요.
2. GitHub → Settings → Developer settings → **Fine-grained personal access tokens** → Generate new token
   - Repository access: **Only select repositories** → 위 비공개 저장소 하나만
   - Permissions → Repository permissions → **Contents: Read and write**
3. Streamlit Secrets에 추가합니다.

```toml
LIBRARY_REPO = "ikim111/snu_translate_library"
GITHUB_TOKEN = "github_pat_..."
```

연결하면 번역이 끝날 때마다 논문 전체(번역한 쪽 + 아직 번역 안 한 쪽은 원문)가 자동 저장되고,
같은 PDF를 다시 올리면 서재의 번역을 불러와 번역비 없이 바로 받을 수 있습니다.

## 번역 엔진 고르기

| | DeepL | OpenAI |
|---|---|---|
| 비용 | Developer 플랜 1백만 자 무료 크레딧, 이후 유료 플랜 | 쓴 만큼 선불 (`gpt-6-luna`는 논문 한 편에 수십 원) |
| 장점 | 문장 누락이 거의 없음, 태그 보존이 확실, 빠름 | 문맥 반영, 학술 문체, 용어집을 지시문으로 반영 |
| 주의 | 용어집이 언어쌍에 따라 안 될 수 있음 | 가끔 문장 누락·태그 유실 → 자동 검사 후 한 번 다시 번역 |

모델과 가격은 `engines.py`의 `OPENAI_MODELS`에서 바꿀 수 있습니다.

## 현재 한계

- 스캔 PDF(글자가 이미지인 PDF)는 OCR이 없어 번역할 수 없습니다.
- 그림·표 안의 글자(축 이름, 범례, 표 칸)는 번역하지 않고 원문 이미지로 둡니다.
- 읽기 순서는 1단·2단 논문 기준입니다. 3단, 박스 기사, 단을 가로지르는 표는 순서가 어긋날 수 있습니다.
- 참고문헌은 "References / Bibliography / 참고문헌" 제목으로 감지합니다. 번호식([1], [2]) 목록은 감지가 불완전합니다.
