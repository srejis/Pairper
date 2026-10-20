# 구조·개발·검증 안내

[제품 소개](../README.md) · [사용자 설치](install.md) · [MCP 연결](mcp.md)

이 문서는 기존 개발자용 README의 기술 정보를 분리한 것입니다. 아래 과거 보고서의 테스트 수와 기능 범위는 각 보고서 작성 시점의 기록이며, 현재 모든 환경에 대한 보증이 아닙니다.

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
| `mcp/server.py` | GUI 독립 stdio MCP, 재구성·업데이트·번역 도구, JSON Schema 리소스, 업데이트 프롬프트 |
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

이번 UI 개편의 파일 구조, 원본 대조 검증 범위와 실제 화면 캡처는 [UI 개편 보고](ui-redesign-report.md)에 정리했습니다. UI 글꼴은 Inter·Pretendard를 로컬 번들링하며 [출처와 라이선스](fonts.md)를 함께 배포합니다.

최신 검증 범위는 [트레이·자동 재연결 보고](tray-supervisor-report.md)에 정리했습니다. 실제 트레이 숨김 중 원격 MCP 목록 조회, 터널 강제 종료 후 자동 복구, 복구 후 원격 목록/페이지 PNG 응답까지 확인했습니다. Windows 자동 시작 작업의 등록·해제와 상태 조회를 확인했으며 실제 재로그인은 수행하지 않았습니다. 이 검증이 실제 논문 재구성의 품질 검증을 뜻하지는 않습니다.

## 프론트엔드 개발

`frontend/`에서 `npm ci` 후 `npm run dev`를 실행하고, 다른 콘솔에서 `uv run pairper --dev-ui http://127.0.0.1:5173/`로 엽니다. 개발 서버는 명시한 루프백 origin에만 허용됩니다. 브라우저만으로는 Qt 브리지가 없어 라이브러리에 접근할 수 없습니다. 배포용 변경은 `npm run build`로 `src/pairper/web/frontend/`에 반영합니다. Python UI 비교는 `uv run pairper --legacy-ui`로 실행합니다.
