# Pairper MCP 및 Windows 터널 운영

## 프로젝트 설정

기존 `D:\project\18_pairper\.env`가 있으면 그대로 사용합니다. 실행기와 진단기는 파일 내용을 덮어쓰지 않고 현재 사용자만 읽고 쓸 수 있도록 DACL을 제한합니다. 파일이 없을 때만 `.env.example`을 참고해 직접 만드세요.

필수 변수는 `CONTROL_PLANE_API_KEY`와 `CONTROL_PLANE_TUNNEL_ID`이며, 선택 변수는 `PAIRPER_DATA_DIR`입니다. 인증에는 실제 `sk-proj-` 비밀 키를 사용합니다. `key_` Tracking ID는 인증 키가 아닙니다. 비밀값을 채팅, 명령줄, 커밋에 넣지 않습니다.

지원 `.env` 문법:

- UTF-8, 한 줄에 `KEY=value` 하나. 빈 줄과 `#`로 시작하는 주석 허용.
- 변수 이름은 위 세 가지만 허용. 중복 키·빈 값·잘못된 형식은 행 번호로 진단.
- 작은따옴표/큰따옴표로 감싼 한 줄 값 지원. 공백이 있는 경로는 따옴표 필요.
- 인라인 주석, `export`, 여러 줄, escape, 보간은 지원하지 않음. `$()`도 실행하지 않고 리터럴로 취급하며, 키 형식 검사에서 거절됨.
- `PAIRPER_DATA_DIR`은 절대 경로. 생략 시 기본 사용자 Local AppData 라이브러리 사용.

영구 사용자/시스템 환경변수는 변경하지 않습니다. `.env`는 Python 실행기가 읽고 자신의 터널 자식 환경에만 전달합니다. 다른 프로젝트의 OpenAI·MCP·터널 환경 설정은 자식 환경에서 제거합니다.

## 기본 실행: 단일 트레이 앱

```powershell
cd D:\project\18_pairper
.\start-pairper.ps1
# 창을 강제로 띄우지 않고 트레이에서 시작
.\start-pairper.ps1 -Tray
```

기본 실행 파일 위치가 달라졌으면 한 번 명시해 저장합니다.

```powershell
.\test-pairper-tunnel.ps1 -ClientPath 'D:\tools\another-folder\tunnel-client.exe'
```

경로는 `.pairper/settings.json`에서 관리합니다. 실행기/진단기가 같은 설정을 재사용합니다. `.pairper/tunnel-client.yaml`은 공식 `sample_mcp_stdio_local`과 같은 설정이며 JSON 문법의 YAML 1.2입니다. 키 대신 `env:CONTROL_PLANE_API_KEY` 참조를 기록합니다. `main` 채널 명령은 프로젝트 `.venv/Scripts/python.exe -m pairper.mcp.server`로 고정하며 임의 명령으로 수정된 프로필은 실행하지 않습니다. 정상 파일을 반복 실행할 때 다시 쓰지 않습니다.

앱 시작과 동시에 Qt 백그라운드 감독자가 터널을 연결합니다. X 버튼은 트레이 숨김이며, **Pairper 열기**, **연결 상태**, **다시 연결**, **Pairper 종료** 메뉴를 제공합니다. 두 번째 실행은 기존 창을 복원합니다. 트레이를 사용할 수 없는 환경에서는 창을 숨기지 않고 정상 종료합니다.

앱과 함께 동작하는 감독자는 10초마다 프로세스/health/readiness를 검사합니다. 3회 연속 실패 또는 프로세스 종료 시 소유한 이전 트리를 정리하고 3·5·10·20·40·60초(지터 포함, 최대 60초)로 재연결합니다. 키/권한/Tunnel ID/필수 파일 오류는 자동 재시도를 멈추며 트레이나 React의 **다시 연결**로 설정을 재검사할 수 있습니다.

`test-pairper-tunnel.ps1`의 `doctor`는 개발·진단용 설정/실행 경로 검사입니다. doctor 성공만으로 원격 인증이나 ChatGPT 호출이 성공한 것은 아닙니다. 실행 중인 앱의 상태는 다음과 같이 확인할 수 있습니다.

```powershell
.\test-pairper-tunnel.ps1 -StatusOnly
```

실제 관리 UI 주소는 이 결과의 `admin_url`에 표시합니다. health/admin은 `127.0.0.1:0`으로 동적 포트를 배정받아 `.pairper/health.url`에 기록하므로 다른 프로젝트의 기본 포트와 충돌하지 않습니다. `/healthz`는 프로세스 건강 상태, `/readyz`는 런타임 준비 상태이며 구분하여 조회합니다. GUI의 **연결 진단**으로 실행 중인 로컬 관리 UI를 열 수 있습니다.

앱은 프로젝트별 QLockFile과 현재 사용자용 로컬 IPC로 단일 실행을 보장합니다. 터널 세션도 별도 파일 잠금으로 중복을 막습니다. Windows Job Object가 소유한 터널과 MCP 자식을 묶고, 명시적 종료 시 프로세스 트리 종료를 확인합니다. 다른 프로젝트 프로세스를 검색해서 일괄 종료하지 않습니다.

`start-pairper-tunnel.ps1`은 앱을 완전히 종료한 상태에서 사용하는 개발·진단용 stdio 터널 실행기로 남아 있습니다. 평소에는 실행할 필요가 없습니다. 앱과 분리 실행기를 동시에 켜면 소유권 충돌 상태를 표시하며 임의로 기존 연결을 빼앗지 않습니다.

로그는 `.pairper/doctor.log`, `.pairper/tunnel.log`이며 키와 터널 식별자를 마스킹합니다. `.env`, 프로필, 로그는 Git에 포함하지 않습니다. 임시 인증 실패가 아닌 영구적인 401/403/인증 오류를 감지하면 상태를 남기고 자동 무한 재시도를 중단합니다.

## 로그인 자동 시작

앱 상단 **설정 → 로그인 시 자동 실행**에서 켜거나 끕니다. 설정을 열 때 Windows 예약 작업의 실제 상태를 조회하며, 변경 중에는 스위치를 잠급니다. 변경 실패 시 오류를 표시하고 기존 표시 상태를 유지합니다. 끄면 실행 중인 앱이나 터널을 중지하지 않고 다음 로그인 시 자동 실행만 해제합니다. 앱 실행만으로 자동 등록하지 않습니다. 아래 스크립트로도 관리할 수 있습니다.

```powershell
.\install-pairper-autostart.ps1 -ValidateOnly  # 등록하지 않고 설정만 확인
.\install-pairper-autostart.ps1               # 현재 사용자용 등록
# 기존 터널 전용 작업이 있을 때만 명시적으로 전환 승인
.\install-pairper-autostart.ps1 -MigrateLegacyTunnelTask
.\uninstall-pairper-autostart.ps1             # 등록만 해제
```

새 작업 이름은 `Pairper`입니다. 로그인 20초 후 `start-pairper.ps1 -Tray`로 앱 전체를 시작합니다. 창 표시를 원하면 설치 시 `-ShowWindow`를 지정하세요. 작업 디렉터리와 스크립트는 절대 경로이며 일반 사용자 권한, 중복 실행 무시, 앱 비정상 종료 시 1분 간격 최대 3회 재시작, 실행 시간 제한 없음입니다. 터널 장애는 앱 내부 감독자가 별도로 복구합니다. 키는 작업에 저장하지 않습니다.

이전 `Pairper MCP Tunnel` 작업이 있으면 현재 프로젝트와 사용자 소유인지 확인합니다. `-MigrateLegacyTunnelTask`를 명시했을 때만 이전 작업을 비활성화하고 실행 중인 이전 작업을 중지합니다. 소유권이 불명확한 작업은 변경하지 않으며 승인 없이 전환하지 않습니다.

설치 후 로그아웃/재로그인하여 트레이 자동 실행 → readiness → ChatGPT 도구 호출 → 중복 프로세스 없음 → 기존 데이터 유지를 확인해야 합니다. 이 검증 때문에 사용자 세션을 자동으로 종료하지 않습니다. 등록 해제는 현재 앱을 종료하거나 다른 작업을 변경하지 않습니다.

## ChatGPT 연결과 검증 단계

[OpenAI 공식 Secure MCP Tunnel 문서](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) 및 설치된 v0.0.16의 도움말을 확인했습니다. 사용자가 준비한 기존 터널·프로젝트·런타임 키를 재사용합니다. 조직의 터널 관리/사용 권한과 대상 ChatGPT 워크스페이스 연결이 필요합니다.

ChatGPT의 사용자 지정 MCP 등록 화면에서 기존 `pairper-local` 터널을 선택하는 작업은 사용자가 진행합니다. 터널 실행기를 켜 둔 상태에서 다음을 확인하세요.

1. 도구 목록에서 Pairper 13개 도구 발견.
2. `list_papers` 결과와 실제 앱 라이브러리 비교.
3. `get_page_image`의 PNG가 모델에 전달되는지 확인.
4. 테스트 논문의 블록 저장·검증·확정 후 로컬 DB와 앱에서 결과 확인.

**로컬 stdio 테스트, 실제 터널 인증/readiness, ChatGPT 실제 도구 호출은 서로 다른 검증 단계입니다.** UI는 실제 health 확인 후에만 “로컬 터널 런타임 준비 완료”를 표시합니다. 마지막 정상 통신/진단 시각과 터널 경유 성공 도구의 이름·시각을 볼 수 있지만, 호출자가 ChatGPT였다는 사실은 별도 확인해야 합니다.

## 로컬 stdio 직접 연결

터널 없이 로컬 MCP 클라이언트가 사용할 수도 있습니다. GUI와 독립적인 기존 인터페이스를 유지했습니다.

```powershell
.\.venv\Scripts\python.exe -m pairper.mcp.server
```

stdout은 MCP 전용입니다. 도구는 `list_papers`, `list_pending_papers`, `get_paper_manifest`, `get_page_content`, `get_page_evidence`, `get_reconstruction_guide`, `get_page_image`, `get_region_image`, `begin_view_build`, `append_document_blocks`, `validate_view_build`, `finalize_view_build`, `get_build_report`입니다. 서버의 `update_papers` 프롬프트와 `pairper://document-schema` 리소스에 문서 계약이 있습니다.

페이지 번호는 1부터, 좌표는 회전 제거한 PDF point 기준 좌상단 원점입니다. 텍스트는 span refs로 연결하며 수식/표/그림에는 원본 영역을 지정합니다. `source_verified`와 `visual_review_pages`는 검토자의 진술이므로 자동 의미 보존 증명으로 간주하지 않습니다. 작업은 같은 ID로 재개·수정하고 검증 통과 후에만 확정합니다. 원본 PDF 삭제·임의 파일 읽기 도구는 없습니다.

TextRun의 `join`은 한 run 내부의 `space`/`none`/`newline`/`dehyphenate` 정책입니다. 서로 다른 run 경계에는 `before: auto|none|space|newline`을 지정할 수 있습니다. 기본 `auto`는 원래 공백과 문장부호, 원문 좌표를 사용합니다. 의도적인 단어 결합·하이픈 제거는 해당 조각들을 하나의 run으로 묶어 정책을 지정하고 이력을 확인하세요.


## 원본 근거 조회와 복합 영역 계약

`get_paper_manifest.pdf_evidence`는 원본 북마크, XML 메타데이터, 첨부 파일명과 논리 구조 트리 존재 여부를 제공합니다. 태그가 있는 파일은 `requires_structure_reader`로 표시하며 지원하지 않는 논리 트리를 읽은 것으로 간주하지 않습니다.

`get_page_evidence(paper_id, page, section, offset, limit)`의 section은 `summary`, `lines`, `characters`, `paths`, `links`, `annotations`, `table_candidates`입니다. limit은 1–200이며 next_offset이 null이 될 때까지 조회합니다. 문자별 위치와 도형의 세부 그리기 정보를 기존 원문 ID에 대응시키되 sources의 문자열·좌표·ID를 수정하지 않습니다. 캐시는 evidence에 별도로 저장합니다. 표 후보는 PyMuPDF가 추론한 값이며 검증된 셀로 취급하지 않습니다.

`get_reconstruction_guide`와 서버의 업데이트 프롬프트가 프로그램과 AI의 역할을 설명합니다. 프로그램은 원본 근거 추출·명시적 선택 확장·출처 계산·구조 및 수식 문법 검사·렌더링을 담당합니다. 연결된 AI는 원본과 근거를 대조해 논리적 읽기 순서, 영역 역할·포함 관계, 수식·표의 구조와 애매한 전사를 판단합니다.

문서 계약 1.1은 `group`과 `content.group_role`, `parent_id`, `relations`를 제공합니다. 그룹의 순서는 부모 다음에 자손이 연속하는 순서이며 빈 그룹·순환·끊긴 부모·잘못된 관계는 저장 검증에 실패합니다. 그룹은 라벨 외 자식 refs를 다시 선언하지 않습니다. 이전 1.0 문서는 계속 읽을 수 있습니다.

`TextRun.ref_ranges`는 같은 페이지·추출 블록 내에서 first_ref부터 last_ref까지 선택한 명시적 범위입니다. refs와 동시에 지정하지 않습니다. 여러 단·페이지는 구간을 순서대로 나열하며, 프로그램이 범위를 refs로 확장해 저장합니다. source_refs를 생략하면 표시 refs와 실제 원본 영역에서 계산합니다. 잘못된 비어 있지 않은 출처 선언과 누락·중복은 계속 검증합니다.

프롬프트·코드의 인쇄된 기호는 `literal=true`로 표시 정규화를 끌 수 있습니다. 코드는 원본 refs가 있으면 하나의 사각형 영역에 제한되지 않으며, 여러 페이지에 걸친 패널은 그룹과 자식 블록으로 표현합니다.

저장 검증은 UI와 같은 KaTeX 0.19.0을 별도 Qt JavaScript 프로세스에서 실행합니다. 잘못된 LaTeX는 source_verified=true여도 완료되지 않습니다. 이 검사는 문법과 렌더 가능성만 검사하며 원본과 수학적 동치를 증명하지 않습니다. 이전 저장본의 이미지 호환 표시는 유지합니다.

검토한 prompt/example/algorithm 그룹은 `content.source_regions`에 원본 테두리 영역 여러 개를 기록할 수 있습니다. 내부 텍스트가 실제 자식에 표시되는지 검사하고, 이 영역은 벡터 테두리 보존에만 사용하며 이미지 누락 검사를 대신하지 않습니다.
