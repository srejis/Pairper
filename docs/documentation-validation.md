# README와 설치 문서 검증 기록

검증일: 2026-10-20. 대상은 GitHub main의 [bc13cb4](https://github.com/srejis/Pairper/commit/bc13cb4a029bdaf4048463df3da034d9169080b7)이며 이번 변경은 문서만 다룹니다.

## 확인한 근거

- 현재 README, docs/mcp.md, pyproject.toml, uv.lock, .python-version, 실행 스크립트, MCP 서버, 업데이트 로직, 번역 workflow/CLI/실행기, 터널 설정 코드.
- 저장소의 UI 개편 보고와 기존 화면 파일. 이 이미지는 실제 앱에서 촬영한 합성 문서이며 실제 논문 품질 데모로 확대하지 않았습니다.
- 제품 목적·제작 동기·차별화·공개 README 조사에 대한 프로젝트 내부 기록. 내부 Notion 주소나 로컬 개인 경로·터널 정보는 공개 문서에 옮기지 않았습니다.
- [사용자 지정 MCP](https://developers.openai.com/api/docs/guides/custom-mcp-server), [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels), [Codex CLI](https://learn.chatgpt.com/docs/codex/cli), [ChatGPT/Codex 이용 조건](https://learn.chatgpt.com/docs/pricing?codex-pricing-plans=individual), [uv 설치](https://docs.astral.sh/uv/getting-started/installation/) 공식 문서. 기능 제공·메뉴·비용은 변경될 수 있습니다.

## 실제 수행

GitHub 커넥터로 고정 커밋의 관련 파일을 가져와 Windows의 별도 검증 폴더에서 실행했습니다. 기존 설치본·개인 라이브러리·터널 키를 사용하거나 변경하지 않았습니다. 이 사본은 전체 GUI 자산을 포함하는 완전한 배포 사본은 아니므로 GUI 설치 검증으로 간주하지 않습니다.

| 검증 | 결과 |
| --- | --- |
| Python 3.12 + uv sync --locked | 고정 의존성 설치 성공, Pairper 0.1.0. Python 실제 실행 버전 3.12.15 |
| pairper --help | 정상 종료, 문서의 실행/등록 옵션 확인 |
| pairper-translate --help | 정상 종료, 실험·게시·계획 옵션 확인 |
| tests/test_mcp.py + tests/test_updates.py | **9개 통과** |
| 실제 stdio 자식 서버 | 합성 PDF 목록과 PNG 응답 확인 |
| MCP 저장 경로 | 합성 문서 begin/append/finalize와 검증 결과 확인 |
| 제목·업데이트 | 원문 보존·제목 확인·미완료 작업 관련 검사 통과 |
| 문서 링크·명령 | 저장소 내부 대상 존재, 실제 옵션/스크립트와 대조. 외부 문서는 공개 열람으로 확인 |

초기 검증에서는 실행 환경의 임시 폴더/자식 프로세스 제한과 검증 사본의 수식 처리 자산 누락 때문에 실패했습니다. 임시 폴더를 격리하고 누락된 원본 자산을 보완한 뒤 위 9개 검사를 재실행했습니다. 저장소 코드를 고쳐 통과시킨 결과가 아닙니다.

## 이번 작업에서 수행하지 않은 것

- 깨끗한 새 Windows PC의 Git·uv 설치부터 GUI 실행까지 전체 과정.
- 사용자의 실제 ChatGPT 계정에서 새 터널 생성·인증·MCP 등록·읽기/이미지/쓰기 호출.
- 실제 Codex 모델 호출, 한국어 전체 생성·검토, 구독/결제 및 사용량 검증.
- 실제 논문의 재구성·번역·용어/수식 해설 품질 검증, 클릭형 해설 패널 검증.
- 재로그인/재부팅 이후 자동 시작과 장시간 연결 유지.

이 제한 때문에 “모든 유료 플랜 호환”, “전체 비용 0원”, “완전 무오류”, “최신 터널 클라이언트와 검증 완료”라고 표기하지 않습니다. 독자는 [MCP 연결 완료 체크리스트](mcp.md)의 단계를 자신의 계정과 환경에서 확인해야 합니다.

## 공개 기능과 로컬 개발 기록의 차이

GitHub 대상 커밋에는 재구성·번역·통합 업데이트·Codex 번역 실행기가 있습니다. 별도 로컬 개발 기록에 있는 클릭형 용어·수식 해설은 이 저장소에서 확인되지 않아 현재 제공 기능으로 표기하지 않았습니다. 0.1.0은 pyproject의 패키지 버전이며 공식 Release 배포 완료를 뜻하지 않습니다. 프로젝트 전체 LICENSE는 없으므로 무료 이용료 정책을 오픈소스 라이선스라고 표현하지 않습니다.
