# Windows 설치와 첫 논문
[README로 돌아가기](../README.md) · [AI 연결](mcp.md)

## 1. 먼저 준비할 것

- Windows에서 실행하는 소스 설치형 개발 버전입니다. 현재 설치 패키지/공식 GitHub Release는 없습니다.
- [Git for Windows](https://git-scm.com/downloads/win)를 설치하고 PowerShell을 새로 여세요.
- [uv 공식 설치 안내](https://docs.astral.sh/uv/getting-started/installation/)에 따라 uv를 설치하세요. 공식 Windows 설치 명령은 다음과 같습니다.

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

위 명령은 인터넷의 설치 스크립트를 실행합니다. 공식 안내의 다른 설치 방식도 사용할 수 있습니다. 이후 PowerShell을 새로 열고 확인합니다.

```powershell
git --version
uv --version
```

Pairper는 Python 3.11 이상을 요구하며 저장소의 기본 버전은 3.12입니다. 다음 단계에서 uv로 3.12를 설치하므로 Python을 별도로 먼저 설치할 필요는 없습니다.

**AI 연결도 준비해야 합니다.** 로컬 stdio를 지원하고 이미지 응답과 쓰기 도구를 처리하는 AI 클라이언트, 또는 사용자 지정 MCP/터널 연결 권한을 가진 ChatGPT 환경이 필요합니다. 한국어 자동 생성·검토에는 추가로 [Codex CLI](https://learn.chatgpt.com/docs/codex/cli)와 ChatGPT 로그인이 필요합니다. 구독만으로 모든 연결 권한이 보장되지는 않습니다.

## 2. 내려받고 실행하기

원하는 설치 상위 폴더에서 다음을 실행하세요. 특정 드라이브나 기존 개발자 폴더는 필요하지 않습니다.

```powershell
git clone https://github.com/srejis/Pairper.git
cd Pairper
uv python install 3.12
uv sync --locked
uv run pairper --help
uv run pairper
```

성공 기준은 **Pairper 라이브러리 창이 열리는 것**입니다. 창만 열렸다고 MCP 연결이 끝난 것은 아닙니다. 프론트엔드 빌드 결과와 글꼴이 포함되어 있어 일반 실행에는 Node.js/npm이 필요 없습니다.

터널 설정이 없으면 앱의 연결 상태가 미설정으로 표시될 수 있습니다. [AI 연결 안내](mcp.md)에서 로컬 stdio와 ChatGPT 터널 중 사용할 방식을 설정하세요.

스크립트 실행이 허용되는 환경에서는 다음 실행기도 사용할 수 있습니다.

```powershell
.\start-pairper.ps1
```

화면이 비거나 GPU/원격 데스크톱 환경에서 표시가 실패하면:

```powershell
.\start-pairper.ps1 -SoftwareRendering
```

조직의 PowerShell 실행 정책이 스크립트를 차단한다면 정책을 임의로 변경하지 말고 직접 실행 경로인 **uv run pairper**를 사용하세요.

## 3. 한국어 자동 생성 준비

현재 구현은 연결된 대화 모델이 번역 전부를 직접 작성하는 방식 대신, 프로그램이 Codex CLI를 호출해 생성·검토·수정을 관리하는 흐름을 사용합니다.

1. [공식 Codex CLI 설치 안내](https://learn.chatgpt.com/docs/codex/cli)에 따라 **Pairper를 실행하는 Windows 환경에서 사용할 수 있는 CLI**를 설치합니다. WSL 안에만 설치한 실행 파일은 Windows 앱에서 자동으로 사용할 수 있다고 가정하지 마세요.
2. codex를 실행하고 **ChatGPT 계정으로 로그인**합니다. MCP 터널 키를 모델 로그인에 넣지 않습니다.
3. 아래 도움말을 확인합니다. Pairper가 요구하는 격리 실행 옵션을 지원해야 합니다.

```powershell
codex --version
codex exec --help
codex features list
uv run pairper-translate --help
```

현재 실행기는 --ephemeral, --ignore-user-config, --output-schema, --output-last-message, --json과 shell_tool 비활성화 지원을 검사합니다. 옵션이 없으면 번역 실행이 차단됩니다. CLI 버전 번호만으로 호환을 보장하지 않으며, 상세 원인은 workflow 보고서로 확인합니다. Codex 사용 한도에 도달하면 구독/사용량 상태를 확인하세요.

CLI만으로 실행하려면, 원문 뷰가 준비된 뒤 실제 논문 ID로 다음 명령을 사용합니다. 꺾쇠 괄호 부분은 그대로 입력하지 말고 실제 값으로 바꿉니다.

```powershell
uv run pairper-translate <paper_id> --experiment first-korean --publish
```

--publish는 인터넷 게시가 아니라 **로컬 번역 확정**입니다. 같은 실험 이름은 재개에 사용합니다. [번역 시스템 상세](translation-workflow.md)

## 4. 첫 논문 읽기

1. 앱에서 **PDF 추가**를 누르고 전송 권한이 있는 PDF 하나를 선택합니다.
2. 라이브러리에 등록된 제목/파일명과 상태를 확인합니다.
3. AI 연결 안내의 읽기·이미지·쓰기 검증을 마칩니다.
4. AI 대화창에서 “Pairper 논문 업데이트해 줘”라고 요청합니다. 원문 재구성, 제목 확인, 한국어 생성의 결과를 각각 확인하세요.
5. 읽기 가능한 논문을 열고 목차, 원본 확인, 원문/한국어 전환을 확인합니다. 번역이나 제목 작업 실패는 원문 재구성 실패와 구분합니다.

PDF 등록만 선택적으로 수행하는 명령도 있습니다. 경로는 자신의 파일로 교체하세요.

```powershell
uv run pairper --import-pdf '.\paper.pdf'
```

## 데이터 위치와 백업

기본 라이브러리는 Windows의 사용자 Local AppData 아래 Pairper/library에 생성됩니다. 원본 PDF, 추출 자료, 재구성 뷰, 번역과 DB를 보관합니다. 백업은 앱과 관련 MCP 작업을 종료한 뒤 라이브러리 전체를 대상으로 하세요.

사용자 지정 위치는 PAIRPER_DATA_DIR의 **절대 경로**로 지정합니다. 앱과 MCP가 반드시 같은 경로를 사용해야 합니다. ChatGPT 터널은 저장소 .env의 설정을 사용하고, 직접 연결하는 stdio 클라이언트는 해당 클라이언트의 환경 설정에 동일한 값을 넣습니다. .env는 임의의 로컬 MCP 클라이언트에 자동 적용되지 않습니다.

```powershell
# 선택 사항: 실제로 사용할 절대 경로를 확인한 뒤 입력
$env:PAIRPER_DATA_DIR = Read-Host '라이브러리 절대 경로'
uv run pairper
```

경로를 바꾸면 기존 데이터가 자동 이동하지 않습니다. 빈 라이브러리가 보이면 재등록하기 전에 경로 일치를 확인하세요.

## 창 닫기와 종료

X 버튼은 트레이로 숨깁니다. 완전히 종료하려면 트레이의 **Pairper 종료**를 선택하세요. 다시 실행하면 기존 창을 복원합니다. 자동 실행은 기본 등록되지 않으며, 원할 때 **설정 → 로그인 시 자동 실행**에서 켤 수 있습니다.

## 자주 막히는 곳

| 증상 | 확인 |
| --- | --- |
| uv 또는 git을 찾지 못함 | 설치 후 PowerShell을 새로 열고 버전 확인 |
| 의존성 다운로드 실패 | PyPI/Python 다운로드에 대한 네트워크·프록시 정책 확인. 고정 lock 파일을 임의로 바꾸지 않기 |
| 창이 열리지 않음 | 터미널에서 uv run pairper로 오류 확인, 필요 시 SoftwareRendering 실행 |
| 앱과 AI가 다른 논문 목록을 반환 | PAIRPER_DATA_DIR 일치 확인 |
| 재구성은 됐지만 한국어가 없음 | Codex CLI 로그인·필수 옵션·사용 한도와 번역 workflow 보고서 확인 |
| 터널 ready인데 ChatGPT 호출 실패 | ChatGPT 등록/워크스페이스 연결·도구 권한 확인. ready는 원격 호출 성공 증명이 아님 |

이 문서의 실제 수행 범위는 [문서 검증 기록](documentation-validation.md)을 참고하세요.
