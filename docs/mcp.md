# AI/MCP 연결: 로컬 stdio와 ChatGPT 터널
[README](../README.md) · [Windows 설치](install.md)

## 연결 전에 확인하세요

Pairper의 재구성·한국어 생성에는 AI가 필요합니다. PDF 등록과 이미 저장한 문서 열람만으로 새 결과가 생성되지는 않습니다.

두 연결 방식 중 하나를 선택합니다.

| 방식 | 준비 |
| --- | --- |
| 같은 PC의 로컬 MCP 클라이언트 | stdio 지원, 이미지 도구 응답 수신, 저장 도구 실행 권한 |
| ChatGPT에서 로컬 Pairper 사용 | 사용자 지정 MCP 추가/사용 권한 + Secure MCP Tunnel + 해당 워크스페이스 연결 |

한국어 자동 생성·독립 검토는 두 방식 모두 **Windows에서 실행 가능한 Codex CLI와 ChatGPT 로그인**이 추가로 필요합니다. [설치 확인](install.md)

### ChatGPT 플랜과 비용

확인 기준: 2026-10-20. [OpenAI의 사용자 지정 MCP 공식 안내](https://developers.openai.com/api/docs/guides/custom-mcp-server)는 ChatGPT 웹에서의 연결과 워크스페이스 권한·보안 제한, 도구별 승인 설정을 안내합니다. 이 문서만으로 Free/Plus/Pro/Business/Enterprise/Edu 각각의 모든 계정에서 읽기·쓰기·터널 기능이 제공된다고 확정할 수 없습니다. **“유료 구독이면 모두 가능”이라고 가정하지 마세요.**

먼저 자신의 계정에 사용자 지정 MCP 추가 화면이 있는지, 이미지와 쓰기 도구가 허용되는지 확인하세요. 조직 계정은 관리자 정책도 적용됩니다. 터널 권한은 ChatGPT 구독과 별개인 Platform 조직 권한입니다. 메뉴가 없다면 설치 성공만으로 해결되지 않으므로 현재 공식 안내와 관리자에게 접근 가능 여부를 확인하세요.

직접 설치 시 **Pairper 이용료는 무료**입니다. MCP 프로토콜은 모델 이용권이 아니며, Pairper가 MCP 호출마다 별도 모델 API 사용료를 청구하지 않습니다. 현재 ChatGPT 대화와 Codex ChatGPT 로그인 경로는 계정의 구독/사용 한도를 사용합니다. 터널 런타임 키는 연결 인증에 사용하며 모델 추론용 키로 사용하지 않습니다. 터널·외부 인프라의 과금 여부는 제공자 조건을 별도로 확인해야 합니다. 향후 Pairper 운영 공용 서버 MCP는 유료 예정이나 아직 미출시·가격 미정입니다.

## A. 로컬 stdio 직접 연결

터널이나 .env의 터널 인증 정보 없이 사용할 수 있습니다. 클라이언트에 다음 항목을 입력합니다.

- command: 이 저장소 .venv/Scripts/python.exe의 **절대 경로**
- args: -m, pairper.mcp.server
- cwd를 지원하는 클라이언트라면 저장소 루트의 절대 경로
- env: GUI와 동일한 PAIRPER_DATA_DIR. 기본 위치를 함께 쓰면 생략 가능

다음은 설정 형식 예시입니다. YOUR_INSTALL_DIR과 YOUR_LIBRARY_DIR은 **직접 교체하는 자리표시자**이며 실재 경로가 아닙니다. 클라이언트마다 설정 파일과 필드가 다르므로 해당 클라이언트의 MCP 안내를 따르세요.

```json
{
  "mcpServers": {
    "pairper": {
      "command": "YOUR_INSTALL_DIR/Pairper/.venv/Scripts/python.exe",
      "args": ["-m", "pairper.mcp.server"],
      "env": {
        "PAIRPER_DATA_DIR": "YOUR_LIBRARY_DIR"
      }
    }
  }
}
```

기본 라이브러리를 쓰려면 env 항목 전체를 생략합니다. 이 서버는 stdio 프로토콜을 사용하므로 브라우저 URL로 등록할 수 없습니다. PowerShell에서 아래 명령이 입력을 기다리며 조용히 있는 것은 정상일 수 있지만 **도구 호출 성공 검증은 아닙니다**.

```powershell
.\.venv\Scripts\python.exe -m pairper.mcp.server
```

수동 실행은 Ctrl+C로 종료하고 MCP 클라이언트가 서버를 시작하도록 하세요. stdout은 MCP 통신 전용입니다. GUI가 읽는 .env의 데이터 경로는 이 직접 실행에 자동 적용되지 않으므로 env를 맞춰야 합니다.

## B. ChatGPT에서 연결: Secure MCP Tunnel

현재 Pairper 서버는 stdio이며 자체 공개 HTTP 주소를 제공하지 않습니다. localhost 주소를 ChatGPT에 붙이는 대신 공식 Secure MCP Tunnel을 통해 연결합니다. [OpenAI 공식 터널 안내](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

### 1. 터널과 권한 준비

[Platform 터널 설정](https://platform.openai.com/settings/organization/tunnels)에서 사용할 터널을 만들거나 기존 것을 재사용하세요. 생성/수정에는 **Tunnels Read + Manage**, 실행·선택에는 **Read + Use**가 필요합니다. 대상 ChatGPT 워크스페이스와 Platform 조직의 연결을 확인하세요. 보이지 않는다면 조직 소유자/RBAC 관리자에게 권한을 요청합니다.

터널 이름은 자유롭게 정합니다. 개발자의 기존 터널이나 키를 공유받을 필요는 없습니다.

### 2. 공식 tunnel-client 설치

[공식 최신 배포](https://github.com/openai/tunnel-client/releases/latest) 또는 Platform의 다운로드 링크에서 Windows와 CPU에 맞는 실행 파일을 받아 압축을 풉니다. Pairper의 과거 검증은 v0.0.16 기준이었으며 최신 바이너리의 호환을 자동 보장하지 않습니다. 다음 도움말로 시작하세요.

```powershell
$tunnelClientPath = Read-Host '압축을 푼 tunnel-client.exe의 절대 경로'
& $tunnelClientPath help quickstart
```

Pairper 실행기는 프로필을 만들므로 별도의 init/run 프로필을 중복 실행할 필요는 없습니다.

### 3. 이 저장소의 .env 설정

저장소 루트에서 실행합니다. 기존 파일이 있으면 덮어쓰지 않습니다.

```powershell
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
notepad .env
```

메모장에서 아래 두 변수의 빈 값만 자신의 정보로 채워 저장하세요.

- CONTROL_PLANE_API_KEY: 터널 런타임용 실제 프로젝트 비밀 키. 현재 코드가 요구하는 형식은 sk-proj- 접두사입니다. key_ Tracking ID는 사용할 수 없습니다.
- CONTROL_PLANE_TUNNEL_ID: 자신이 만든 터널 ID.
- 선택 사항 PAIRPER_DATA_DIR: 사용자 지정 라이브러리의 절대 경로. 앱과 MCP가 같은 값을 사용해야 합니다.

**비밀값은 채팅·명령 인자·스크린샷·커밋에 넣지 마세요.** .env와 .pairper 폴더는 공개하지 않습니다. 터널 키가 있다는 사실만으로 모델 API를 호출하는 것은 아닙니다.

지원 문법은 UTF-8의 한 줄 KEY=value, 빈 줄, 줄 전체 주석, 한 줄 작은/큰따옴표입니다. 허용 변수는 위 세 개뿐입니다. 빈 값·중복 변수·인라인 주석·export·여러 줄·변수 치환은 지원하지 않습니다. 사용하지 않는 선택 변수는 주석 상태로 둡니다.

### 4. 실행 파일 등록 → 앱 시작 → 로컬 진단

같은 PowerShell에서:

```powershell
.\test-pairper-tunnel.ps1 -ClientPath $tunnelClientPath
.\start-pairper.ps1
```

진단기는 설정과 실행 경로를 검사하고 .pairper/settings.json에 실행 파일 위치를 저장합니다. 앱이 터널을 시작합니다. **첫 진단 통과는 원격 인증 성공이 아닙니다.**

앱을 켜 둔 채 다른 PowerShell에서 저장소 폴더로 이동해 상태를 확인합니다.

```powershell
.\test-pairper-tunnel.ps1 -StatusOnly
```

설정의 **연결 진단** 또는 상태 결과의 admin_url로 로컬 관리 화면을 열어 healthy/ready/connected를 구분해 확인합니다. 관리 화면은 로컬 전용입니다. Pairper 앱은 연결을 유지하고 실패 시 재연결합니다.

### 5. ChatGPT 등록

[ChatGPT Plugins](https://chatgpt.com/plugins)에서 추가 버튼 → **Add custom MCP server** → Connection의 **Tunnel**을 선택합니다. 자신의 터널을 고르고 인증 설정·위험 안내를 검토한 뒤 생성하여 설치합니다. 이 Pairper 서버 자체에는 OAuth 구현이 없으며 터널 연결의 권한으로 접근합니다. 사용할 수 있는 인증 선택지는 실제 화면과 조직 정책을 따르세요.

대화창에서 @로 설치한 Pairper를 선택합니다. 예전 UI의 “Apps/Connectors/Developer mode” 표기가 남아 있다면 최신 공식 연결 안내와 실제 계정 화면을 기준으로 하세요. 터널이 목록에 없으면 워크스페이스 연결과 Read + Use를 확인합니다.

## 연결 완료를 판단하는 순서

전송 가능한 테스트 PDF 한 편으로 확인하세요.

1. **도구 발견:** list_papers, list_paper_updates, get_page_image, begin_view_build, finalize_view_build 및 번역 workflow 도구가 보이는지 확인합니다. 도구 수는 버전에 따라 달라지므로 고정 숫자를 성공 조건으로 삼지 않습니다.
2. **읽기:** “Pairper의 list_papers를 호출해 목록을 보여 줘.” 결과가 앱과 같은지 비교합니다.
3. **이미지:** 해당 논문 ID와 1페이지로 get_page_image를 호출하고 실제 PNG가 AI에 전달되는지 확인합니다. 경로 문자열만 반환되면 완료하지 않습니다.
4. **쓰기:** 테스트 논문의 원문 재구성을 요청하고 begin → append → validate → finalize 결과와 앱 표시를 확인합니다. 쓰기 승인이 필요하면 전송 내용을 읽고 승인하세요. 저장 권한이 없으면 목록 조회만 성공한 것입니다.
5. **한국어:** start_translation_workflow → run_translation_workflow → get_translation_workflow_report → ready 후 finalize_translation_workflow 흐름과 앱의 한국어 전환을 확인합니다. 실제 Codex 호출과 사용 한도가 필요합니다.
6. **결과:** list_paper_updates(include_complete=true)로 원문·제목·한국어의 완료/남은 작업을 확인합니다.

**로컬 stdio 성공 ≠ 터널 readiness ≠ ChatGPT 실제 도구 호출 성공 ≠ 논문 내용 품질 검증.** 앱의 마지막 도구 호출 기록만으로 호출자가 ChatGPT라고 단정하지 않습니다.

## 문제 해결

| 증상 | 확인할 것 |
| --- | --- |
| MCP 메뉴/쓰기 기능 없음 | 계정의 기능 제공 여부, 도구 승인 설정, 워크스페이스 정책 |
| 터널 목록에 없음 | 대상 워크스페이스 연결, Platform 조직, Tunnels Read + Use |
| 실행 파일 없음 | 다운로드·압축 해제 후 -ClientPath로 실제 위치 등록 |
| 401/403 | 런타임 키와 조직 권한 확인. 비밀값을 공유하지 않기 |
| 도구는 보이지만 목록이 다름 | 앱과 서버의 PAIRPER_DATA_DIR 일치 |
| 이미지 실패 | 클라이언트 이미지 응답 지원과 원본 PDF 접근 상태 |
| 새 번역 도구가 안 보임 | 앱/MCP 재시작 후 ChatGPT 앱의 도구 정보 새로 고침 |
| 한국어 생성 실패 | Codex CLI 로그인·필수 격리 옵션·사용 한도, workflow 보고서 |
| 준비 상태지만 호출 실패 | 실제 ChatGPT 대화에서 다시 호출하여 단계별 오류 확인 |

논문 원본과 이미지가 연결한 AI에 전달됩니다. 로컬 보관과 외부 AI 처리는 구분하세요. 로그를 제보할 때 키·식별자·개인 경로·논문 비공개 정보를 가립니다.


## 고급 운영

아래 명령은 저장소 루트에서 실행합니다. 기본 연결이 완료된 뒤 필요할 때만 사용하세요.

## 기본 실행: 단일 트레이 앱

```powershell
.\start-pairper.ps1
# 창을 강제로 띄우지 않고 트레이에서 시작
.\start-pairper.ps1 -Tray
```

기본 실행 파일 위치가 달라졌으면 한 번 명시해 저장합니다.

```powershell
.\test-pairper-tunnel.ps1 -ClientPath $tunnelClientPath
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


## MCP 문서 계약

현재 도구 목록은 클라이언트의 list_tools 응답을 기준으로 확인합니다. 서버의 update_papers 프롬프트와 pairper://document-schema 리소스, 번역 가이드와 workflow 보고서를 읽어 주세요.

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
