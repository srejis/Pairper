# Pairper

AI가 원본 PDF의 구조를 판단하고, 원문 ID와 원본 영역을 연결한 **페이지 구분 없는 1열 논문 뷰**를 저장하는 로컬 데스크톱 앱입니다. UI는 React + TypeScript, 데스크톱 셸과 데이터 처리는 Python입니다. 한국어 번역·설명·질의응답은 포함하지 않습니다.

## 실행

Python 3.11 이상과 [uv](https://docs.astral.sh/uv/)가 필요합니다. 현재 개발 환경에는 uv와 Python 3.12를 설치하고 의존성을 구성했습니다.

```powershell
cd D:\project\18_pairper
uv sync --locked
uv run pairper
```

프론트엔드 빌드 결과가 저장소에 포함되어 있어 Node.js나 Vite 서버 없이 실행할 수 있습니다. 현재 PC에서 PATH를 갱신하지 않고 실행하려면:

```powershell
.\.venv\Scripts\pairper.exe
```

`start-pairper.ps1`로도 실행할 수 있습니다. GPU/원격 데스크톱 환경에서 화면이 비면 `-SoftwareRendering` 옵션을 사용하세요.

```powershell
.\start-pairper.ps1 -SoftwareRendering
```

Pairper 앱이 터널을 자동 연결하고 관리합니다. 기존 `.env`는 수정하지 않고 재사용하며, 터널 콘솔을 따로 켤 필요가 없습니다. X 버튼은 트레이로 숨기고, 완전히 종료하려면 트레이의 **Pairper 종료**를 선택합니다. 앱을 다시 실행하면 기존 창이 복원됩니다.

```powershell
.\start-pairper.ps1 -Tray    # 창을 띄우지 않고 트레이에서 시작
# 필요할 때 상태 확인
.\test-pairper-tunnel.ps1 -StatusOnly
# 사용자가 한 번 실행하면 로그인 시 Pairper 앱 자동 시작 등록
.\install-pairper-autostart.ps1
# 기존 터널 전용 작업이 있다면 전환을 명시적으로 승인
.\install-pairper-autostart.ps1 -MigrateLegacyTunnelTask
# 등록 해제
.\uninstall-pairper-autostart.ps1
```

자격정보 형식, 실행 파일 경로, 상태의 의미는 [터널 운영 안내](docs/mcp.md)를 확인하세요. 앱 상단 **설정 → 로그인 시 자동 실행**에서도 켜거나 끌 수 있습니다. Windows 예약 작업의 실제 상태를 표시하며, 끄면 자동 시작 작업만 해제하고 실행 중인 앱과 MCP 연결은 유지합니다. 앱 실행만으로 자동 시작을 등록하지 않습니다.

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
5. 라이브러리에서 **읽기 가능** 논문을 선택하면 독립된 리더로 이동합니다. 미처리 논문은 행을 펼쳐 상태를 확인하고 요청 문구를 복사합니다. 외부 프로세스의 변경은 1.5초 간격으로 감지합니다. 완료 논문은 다음 업데이트 대상에서 제외됩니다.

라이브러리에서 제목·파일명 검색, 정렬, 상태 필터를 함께 사용합니다. 리더에서는 목차와 원본 대조를 오른쪽 오버레이로 열며, 문서 폭과 읽던 위치를 유지합니다. 문서 블록에 마우스를 올리거나 키보드로 초점을 이동하면 **원본 확인**이 나타납니다. 글자 설정은 15–21px 범위로 조절하며 기본값은 17px입니다. 자동 실행·MCP 상태·재연결·연결 진단은 라이브러리 상단 **설정**에서 관리합니다.

## 구현 구조

| 모듈 | 역할 |
| --- | --- |
| `app/controller.py`, `instance.py` | 트레이, 단일 실행 잠금/IPC, 앱 및 연결 종료 순서 |
| `ui/` | Qt 셸, 제한된 비동기 WebChannel, 네이티브 파일 선택 |
| `frontend/src/` | React + TypeScript 앱 셸, 라이브러리, 12종 문서 블록 |
| `tunnel/supervisor.py`, `session.py` | 10초 상태 확인, 자동 재연결, 소유한 프로세스 트리 관리 |
| `tunnel/config.py`, `runtime.py` | 프로젝트 .env, 프로필, 진단 CLI·안전한 상태·로그 마스킹 |
| `pdf/source.py` | 영구 보관하는 span ID·문자열·좌표·서식·방향, 이미지/벡터 위치, 영역 렌더링 |
| `storage/library.py` | SHA-256 중복 방지, SQLite WAL/트랜잭션, 재개 가능한 작업, 원자적 뷰 확정 |
| `document/models.py` | Pydantic 문서 계약: 12종 블록, 인라인 수식, 병합 셀, 생략 근거 |
| `document/renderer.py` | 원문 ID 기반 HTML, 정규화 변경 이력, 원본 대조 |
| `document/presentation.py`, `text.py` | 버전이 있는 표시 데이터와 공통 원문 연결 정책 |
| `document/comparison.py` | 저장된 블록·영역 ID 기반 원본 PDF 대조, 좌표·해시·이미지 크기 검증 |
| `validation/checks.py` | 원문 누락/중복, 잘못된 참조, 해시, 시각 영역, 표 겹침 검사 |
| `mcp/server.py` | GUI 독립 stdio MCP, 13개 도구, JSON Schema 리소스, 업데이트 프롬프트 |
| `web/` | 로컬 CSS/JS, KaTeX 0.19.0 및 글꼴 |

Python 모듈은 `src/pairper/`, 프론트엔드 소스는 `frontend/` 아래에 있습니다. 데이터는 `originals/`, `sources/`, `views/`, `assets/`, `evidence/`, `staging/`, `library.db`로 구분합니다. DB가 작업의 기준 데이터이며 staging JSON은 복구용 스냅샷입니다. 각 뷰의 `.normalization.json` 및 표시 데이터의 `normalization_audit`에는 줄 결합 전 원문과 결과를 보존합니다.

## 검증

```powershell
uv run pytest -q
uv run ruff check src tests tools
uv run python tools/verify_ui.py
cd frontend
npm ci
npm test
npm run build
```

UI 통합 검증은 Windows에서 합성 단위 테스트용 PDF로 12종 블록, 수식 성공/실패, 병합 셀, 원본 자산, 실행 중 DB 변경 감지를 확인하고 `tmp/ui-verification/`에 결과와 캡처를 씁니다. 지정된 arXiv 논문의 대체 통합 검증으로 간주하지 않습니다.

이번 UI 개편의 파일 구조, 원본 대조 검증 범위와 실제 화면 캡처는 [UI 개편 보고](docs/ui-redesign-report.md)에 정리했습니다. UI 글꼴은 Inter·Pretendard를 로컬 번들링하며 [출처와 라이선스](docs/fonts.md)를 함께 배포합니다.

최신 검증 범위는 [트레이·자동 재연결 보고](docs/tray-supervisor-report.md)에 정리했습니다. 실제 트레이 숨김 중 원격 MCP 목록 조회, 터널 강제 종료 후 자동 복구, 복구 후 원격 목록/페이지 PNG 응답까지 확인했습니다. Windows 자동 시작 작업의 등록·해제와 상태 조회를 확인했으며 실제 재로그인은 수행하지 않았습니다. 이 검증이 실제 논문 재구성의 품질 검증을 뜻하지는 않습니다.

## 프론트엔드 개발

`frontend/`에서 `npm ci` 후 `npm run dev`를 실행하고, 다른 콘솔에서 `uv run pairper --dev-ui http://127.0.0.1:5173/`로 엽니다. 개발 서버는 명시한 루프백 origin에만 허용됩니다. 브라우저만으로는 Qt 브리지가 없어 라이브러리에 접근할 수 없습니다. 배포용 변경은 `npm run build`로 `src/pairper/web/frontend/`에 반영합니다. Python UI 비교는 `uv run pairper --legacy-ui`로 실행합니다.
