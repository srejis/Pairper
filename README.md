# Pairper

AI가 원본 PDF의 구조를 판단하고, 원문 ID와 원본 영역을 연결한 **페이지 구분 없는 1열 논문 뷰**를 저장하는 로컬 데스크톱 앱입니다. 한국어 번역·설명·질의응답은 포함하지 않습니다.

## 실행

Python 3.11 이상과 [uv](https://docs.astral.sh/uv/)가 필요합니다. 현재 개발 환경에는 uv와 Python 3.12를 설치하고 의존성을 구성했습니다.

```powershell
cd D:\project\18_pairper
uv sync --locked
uv run pairper
```

현재 PC에서 PATH를 갱신하지 않고 실행하려면:

```powershell
.\.venv\Scripts\pairper.exe
```

`start-pairper.ps1`로도 실행할 수 있습니다. GPU/원격 데스크톱 환경에서 화면이 비면 `-SoftwareRendering` 옵션을 사용하세요.

```powershell
.\start-pairper.ps1 -SoftwareRendering
```

저장소 기본 위치는 `platformdirs.user_data_path("Pairper", appauthor=False) / "library"`이며 Windows에서는 사용자 Local AppData 아래입니다. GUI와 MCP에 **같은 절대 경로**를 지정해야 합니다.

```powershell
$env:PAIRPER_DATA_DIR = 'D:\my-pairper-library'
uv run pairper
# GUI 없이 등록하는 선택적 CLI
uv run pairper --import-pdf 'C:\papers\paper.pdf'
```

## 사용 순서

1. 앱의 **PDF 추가**에서 하나 또는 여러 PDF를 선택합니다. 등록은 원본 복사·추출만 수행하며 AI를 호출하지 않습니다.
2. [MCP 연결 안내](docs/mcp.md)에 따라 로컬 클라이언트 또는 지원되는 ChatGPT 환경에 연결합니다.
3. 연결된 GPT에 **“Pairper 논문 업데이트해 줘”**라고 요청합니다.
4. GPT가 텍스트와 페이지 이미지를 함께 검토하고, 원문 참조 블록을 저장·검증·확정합니다.
5. 앱 목록에서 논문을 선택합니다. 외부 프로세스의 변경은 1.5초 간격으로 감지합니다. 완료 논문은 다음 업데이트 대상에서 제외됩니다.

## 구현 구조

| 모듈 | 역할 |
| --- | --- |
| `app/`, `ui/` | 실행, 다중 PDF 등록, 목록/상태, QtWebEngine 읽기 화면 |
| `pdf/source.py` | 영구 보관하는 span ID·문자열·좌표·서식·방향, 이미지/벡터 위치, 영역 렌더링 |
| `storage/library.py` | SHA-256 중복 방지, SQLite WAL/트랜잭션, 재개 가능한 작업, 원자적 뷰 확정 |
| `document/models.py` | Pydantic 문서 계약: 12종 블록, 인라인 수식, 병합 셀, 생략 근거 |
| `document/renderer.py` | 원문 ID 기반 HTML, 정규화 변경 이력, 원본 대조 |
| `validation/checks.py` | 원문 누락/중복, 잘못된 참조, 해시, 시각 영역, 표 겹침 검사 |
| `mcp/server.py` | GUI 독립 stdio MCP, 11개 도구, JSON Schema 리소스, 업데이트 프롬프트 |
| `web/` | 로컬 CSS/JS, KaTeX 0.16.22 및 글꼴 |

실제 모듈은 `src/pairper/` 아래에 있습니다. 데이터는 `originals/`, `sources/`, `views/`, `assets/`, `staging/`, `library.db`로 구분합니다. DB가 작업의 기준 데이터이며 staging JSON은 복구용 스냅샷입니다. 각 뷰의 `.normalization.json`에는 줄 결합 전 원문과 결과를 보존합니다.

## 검증

```powershell
uv run pytest -q
uv run ruff check src tests tools
uv run python tools/verify_ui.py
```

UI 통합 검증은 Windows에서 합성 단위 테스트용 PDF로 12종 블록, 수식 성공/실패, 병합 셀, 원본 자산, 실행 중 DB 변경 감지를 확인하고 `tmp/ui-verification/`에 결과와 캡처를 씁니다. 지정된 arXiv 논문의 대체 통합 검증으로 간주하지 않습니다.

현재 검증 범위와 한계는 [구현 결과 보고](docs/implementation-report.md)를 확인하세요. 테스트 논문 `arXiv:2412.15750v1` PDF는 아직 제공되지 않았습니다. 해당 파일과 ChatGPT 터널/계정 연결이 필요하므로, 실제 GPT의 시각 기반 논문 재구성과 ChatGPT 연결은 아직 미검증입니다.
