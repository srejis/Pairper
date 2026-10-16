# Pairper UI 1차 통합 개편 보고

2026-10-16 구현·검증. 초기 화면은 라이브러리이며 유효한 저장 뷰를 선택할 때만 독립된 리더로 전환합니다. 실행 중인 앱도 새 코드로 다시 시작했고 실제 기존 라이브러리와 MCP 연결을 확인했습니다.

## 구현 파일과 책임

| 파일 또는 폴더 | 변경 내용 |
| --- | --- |
| `frontend/src/App.tsx` | library/reader 전환, 라이브러리 변경 구독, PDF 등록 알림. 리더는 필요할 때 로드 |
| `components/library/LibraryScreen.tsx` | 제목·파일명 검색, 3종 정렬, 상태 필터 조합, 80px 논문 행, 미처리 행 확장 및 요청 복사 |
| `components/reader/ReaderScreen.tsx` | 56px 툴바, 연속 1열 문서, 15–21px 글자 설정, 버전이 있는 로컬 UI 설정 저장 |
| `components/reader/TableOfContents.tsx` | 실제 heading 블록에서 생성, 현재 섹션 강조 및 이동 |
| `components/reader/SourcePanel.tsx` | 페이지·영역별 원본 대조, 이미지 확대·축소, 실패 상태 |
| `components/settings/SettingsPanel.tsx` | 로그인 자동 실행, MCP 상태, 다시 연결, 연결 진단과 접힌 상세 정보 |
| `components/common/Overlay.tsx`, `Icon.tsx` | 공통 아이콘, native dialog의 키보드 포커스 제한, ESC·바깥 클릭·닫기, 포커스 복귀, 200ms 애니메이션과 reduced-motion |
| `hooks/useConnection.ts` | 연결 이벤트 구독 및 상태 조회. 화면 전환과 독립적인 앱 연결 생명주기 유지 |
| `styles/` 및 `styles.css` | 디자인 토큰, 라이브러리·리더 반응형 스타일, 터치·키보드 원본 확인 |
| `components/blocks.tsx` | 기존 12종 렌더러 재사용, hover/focus 원본 확인 및 선택 강조. 기존 KaTeX·병합 셀·이미지 fallback 유지 |
| `src/pairper/document/presentation.py` | 기존 표시 모델에 읽기 전용 source_regions/source_error 추가, 저장 뷰 로딩 재사용 |
| `src/pairper/document/comparison.py` | 저장된 블록의 실제 region/source_refs를 검증하고 제한된 원본 PNG 생성 |
| `src/pairper/ui/bridge.py`, `frontend/src/bridge.ts`, `types.ts` | 제한된 원본 조회, 실제 등록일·원본 존재·저장 오류 조회, 네이티브 요청 문구 복사 |
| `frontend/src/assets/fonts/`, `frontend/public/fonts/` | 로컬 Inter·Pretendard와 전체 배포 라이선스 |
| `tests/test_comparison.py`, `frontend/src/App.test.tsx`, `test-setup.ts` | 원본 대조 검증과 화면 전환·설정·접근성 회귀 검증 |
| `tools/verify_ui.py` | 실제 QtWebEngine/WebChannel 통합 검증과 화면 캡처 |
| `start-pairper.ps1` | 콘솔 없는 pythonw의 GUI 창을 Windows Hidden 시작 플래그로 가리지 않도록 변경. 트레이 시작 여부는 기존 --tray로 제어 |

기존 `PaperLibrary.tsx`와 `PaperReader.tsx`의 동시 배치 구현은 위 화면별 컴포넌트로 교체했습니다. 배포용 `src/pairper/web/frontend/`도 다시 빌드했습니다. 구현 지시서는 `프롬프트/2026-10-16_UI-전면개편_1차-통합-구현지시서.md`에 보관했습니다.

## 원본 대조의 동작과 경계

- 프론트엔드는 paper/view/block/region ID만 전달합니다. 파일 경로, 좌표, 렌더링 배율 또는 실행 명령을 전달하지 않습니다.
- 현재 유효한 저장 뷰와 원본 SHA-256을 확인합니다. 오래된 뷰, 알 수 없는 ID, 경로 입력, 잘못된 좌표는 거부합니다.
- 실제 PDF 페이지 경계도 검증하므로 추출 메타데이터의 페이지 크기를 잘못 바꿔도 PDF 밖으로 렌더링하지 않습니다.
- 텍스트는 페이지·블록·줄을 구분하고 같은 줄의 인접 span만 합칩니다. 다른 줄·단·페이지의 텍스트를 하나의 거대한 bbox로 합치지 않습니다. 시각 블록 및 인라인 수식의 저장된 region도 탐색할 수 있습니다.
- PNG는 배율 최대 2배, 긴 변 약 1600px, 약 2MP, 인코딩 4MB 이하로 제한합니다. 너무 큰 영역은 오류로 처리합니다. 생성 이미지는 라이브러리 assets 내부에만 저장합니다.
- 원본 확인 가능 여부는 검증된 영역 정보에 따르며, 원본 위치가 잘못된 블록에는 실패 안내를 표시합니다. 조회 실패 시 추측 이미지나 다른 페이지를 표시하지 않습니다.

## 검증 결과

| 항목 | 결과 |
| --- | --- |
| Python 전체 테스트 | 68개 통과: 기존 테스트 및 새 원본 대조·라이브러리 표시 테스트 |
| React 테스트 | 20개 통과: 기존 12종 렌더러 및 화면 전환·검색/정렬/필터·인라인 확장·설정·원본 대조·포커스·글자 설정 |
| 정적 검사·배포 빌드 | Ruff, TypeScript, Vite 통과. 리더/KaTeX를 분리 로드하여 초기 JS 약 236kB, 리더 약 270kB |
| 실제 Qt PDF 등록·요청 복사 | 네이티브 파일 선택 → 제한된 브리지 → 원본 복사·추출·행 갱신 확인. Qt 클립보드 복사 성공 및 테스트 전 클립보드 복원 |
| 실제 Qt 문서 렌더링 | 합성 PDF의 15개 블록/12종 유형, KaTeX 2개, 잘못된 수식 fallback 1개, colspan=2 표, 깨진 이미지 0개, 로컬 폰트 로딩 확인 |
| 실제 Qt 목차·패널 | 목차 이동·현재 항목, 패널 전후 문서 폭/블록 높이/스크롤 위치 일치, ESC·바깥 클릭·닫기와 포커스 복귀 확인 |
| 실제 Qt 여러 페이지 원본 대조 | 한 블록이 PDF 1·2페이지에 걸친 합성 문서에서 영역 전환·PNG 로딩 확인. 별도 단위 테스트로 여러 줄·2단 본문 분리와 변조 거부 확인 |
| 실제 Qt 반응형·다시 열기 | 1280×850 및 480×760 캡처. 좁은 툴바 겹침 없음. 리더 재진입 후 15개 블록 유지 |
| 실제 트레이·MCP | 새 코드의 ApplicationController 시작, 숨긴 상태에서 원격 list_papers 성공. 중복 실행 종료 코드 0, 기존 창 복원, 터널 PID 유지 |
| 실제 터널 장애 복구 | 소유한 터널을 종료한 뒤 새 프로세스로 자동 복구, 복구 후 원격 list_papers 성공. 종료 시 supervisor stopped 및 트레이 제거 확인 |
| 최종 앱 실행 | 수정한 `start-pairper.ps1 -SoftwareRendering`으로 새 앱 실행. Windows의 Pairper 창 생성 확인, connected/ready/healthy 모두 정상, 실제 원격 list_papers 재확인 |
| 기존 데이터 보존 | 실제 라이브러리 1편의 ID·파일명·상태·SHA-256·current_view_id 및 원본 파일 해시가 작업 전후 동일 |
| 로그인 자동 실행 | 기존 Pairper 예약 작업 Ready 유지. 설정에서 실제 상태를 표시. 재부팅·재로그인은 수행하지 않음 |

## 실제 화면 캡처

아래 캡처의 논문 내용은 UI 검증용 합성 문서입니다.

- [라이브러리 1280×850](screenshots/ui-redesign/library-1280.png)
- [미처리 논문 인라인 확장](screenshots/ui-redesign/library-pending-1280.png)
- [리더 1280×850](screenshots/ui-redesign/reader-1280.png)
- [목차 오버레이](screenshots/ui-redesign/toc-1280.png)
- [원본 대조](screenshots/ui-redesign/source-1280.png)
- [여러 페이지 원본 대조](screenshots/ui-redesign/source-multipage-1280.png)
- [리더 480×760](screenshots/ui-redesign/reader-480.png)
- [좁은 화면 목차](screenshots/ui-redesign/toc-480.png)
- [좁은 화면 라이브러리](screenshots/ui-redesign/library-480.png)
- [좁은 화면 설정](screenshots/ui-redesign/settings-480.png)

## 호환성과 남은 검증 범위

원본 PDF, SQLite 문서·작업 데이터, 저장 문서 스키마와 MCP 11개 도구의 계약은 변경하지 않았습니다. 트레이 상주·자동 재연결·자동 시작 설정을 유지했습니다. 정상 연결의 기술 정보는 설정 안으로 이동했고 상시 하단 상태줄은 제거했습니다. 성공 알림은 5초 뒤 사라지고 중요한 실패 알림은 닫을 때까지 남습니다.

현재 실제 arXiv 논문에는 유효한 재구성 뷰가 없어 리더·원본 대조의 시각 검증은 합성 문서로 수행했습니다. 실제 논문 전체의 재구성 품질·원문 충실도를 검증한 결과로 해석하면 안 됩니다. 사용자 환경의 재로그인 자동 시작과 실제 논문 재구성 완료 후의 장시간 읽기 검증은 남아 있습니다. 최종 재시작 직후 원격 확인 1회가 실패했으나 다음 요청은 정상 응답했습니다. 로컬 readiness와 원격 호출은 구분해 확인했습니다. 한국어 번역·AI 설명·채팅 패널은 요청 범위에 따라 추가하지 않았습니다.

글꼴 원본과 재배포 고지는 [폰트 안내](fonts.md)를 참고하세요.
