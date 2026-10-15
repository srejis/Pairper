# MCP 실행과 연결

## 로컬 stdio

GUI를 켜지 않아도 동작합니다. stdout은 MCP 전용이며 오류 로그는 stderr로 전달됩니다.

```powershell
uv run pairper-mcp
# 또는
.\.venv\Scripts\python.exe -m pairper.mcp.server
```

stdio 서버는 MCP 클라이언트가 프로세스를 실행해 입출력 파이프로 연결합니다. 콘솔에서 단독 실행한 뒤 출력이 없고 기다리는 것은 정상입니다.

로컬 MCP 클라이언트 설정 예시:

```json
{
  "mcpServers": {
    "pairper": {
      "command": "D:\\project\\18_pairper\\.venv\\Scripts\\python.exe",
      "args": ["-m", "pairper.mcp.server"],
      "env": {
        "PAIRPER_DATA_DIR": "C:\\Users\\sreji\\AppData\\Local\\Pairper\\library"
      }
    }
  }
}
```

실제 라이브러리 경로는 앱 상태 표시줄과 **MCP 연결 방법** 버튼에서 확인하세요. 위 예시의 경로는 사용자 설정에 맞게 바꿉니다. 임의 파일 읽기·원본 삭제·덮어쓰기 도구는 제공하지 않습니다.

공식 [MCP Python SDK](https://py.sdk.modelcontextprotocol.io/)와 설치된 API를 확인해 `mcp==2.3.0` 및 `MCPServer`를 사용했습니다. 전체 전이 의존성은 `uv.lock`으로 고정했습니다.

## GPT 작업 계약

첫 도구는 `list_pending_papers`입니다. 서버의 `update_papers` 프롬프트 및 `pairper://document-schema` 리소스를 읽습니다. 도구는 다음 11개입니다.

`list_papers`, `list_pending_papers`, `get_paper_manifest`, `get_page_content`, `get_page_image`, `get_region_image`, `begin_view_build`, `append_document_blocks`, `validate_view_build`, `finalize_view_build`, `get_build_report`.

- 페이지 번호는 1부터 시작합니다. 좌표는 회전을 제거한 PDF point 좌표이며 좌측 상단이 원점입니다. `rotation`은 원본 영역 이미지를 읽기 방향으로 회전하는 옵션입니다.
- span ID는 최초 추출 결과와 함께 영구 저장합니다. 재추출은 자동 수행하지 않습니다.
- 텍스트는 `content.runs = [{"kind":"text","refs":["실제 span ID"],"join":"space"}]`로 전달합니다. `none`, `newline`, `dehyphenate`도 지원합니다.
- 본문 문자열·HTML·임의 파일 경로는 문서 계약에 없습니다. 인라인 수식도 원본 영역과 refs를 연결합니다.
- `source_refs`에는 실제 텍스트 참조와 해당 블록의 모든 원본 영역에 완전히 포함되는 span의 합집합을 넣습니다. 거짓 참조 선언만으로 누락 검사를 통과할 수 없습니다.
- 표 셀은 `row`, `column`, `rowspan`, `colspan`, `runs`로 구성합니다. 셀 숫자도 원문 refs로만 표시합니다.
- `source_verified=false`인 수식/표는 원본 영역 이미지로 표시합니다. `true`는 검토자의 대조 진술이며 코드가 의미적 동일성을 증명한 것은 아닙니다. LaTeX 구문 오류는 브라우저에서 원본 이미지로 대체합니다.
- 그림/수식/표/불확실 영역에는 `content.region`이 필수입니다. `asset_id`는 서비스가 결정합니다.
- `visual_review_pages`는 실제 이미지를 확인한 페이지에만 기록하세요. PNG가 도구 응답으로 반환된다는 사실과 모델이 이미지를 읽었다는 사실은 다릅니다.
- 같은 작업은 `begin_view_build`로 재개하고, 블록은 같은 ID로 교체합니다. 배치 재시도는 멱등적입니다. `get_build_report`에서 현재 전체 문서를 볼 수 있습니다.
- 잘못 추가한 블록이나 생략 기록은 `append_document_blocks`의 `remove_block_ids`, `remove_omission_refs`로 제거할 수 있습니다. 확정된 뷰에는 적용할 수 없습니다.
- 렌더러가 페이지 이미지 뷰어로 대체되지 않도록, 텍스트가 추출된 페이지의 전체 영역 fallback은 거절합니다. 스캔 페이지는 전체 원본 이미지 보존을 허용하고 텍스트 추출 불가 사실을 manifest에 표시합니다.

## ChatGPT와 Secure MCP Tunnel

로컬 stdio 테스트 통과는 ChatGPT 연결 성공을 뜻하지 않습니다. 이 환경에는 Pairper용 터널 ID·런타임 자격증명·ChatGPT 플러그인 연결이 준비되어 있지 않습니다.

아래는 2026-10-15에 확인한 [OpenAI 공식 Secure MCP Tunnel 안내](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)에 따른 설정 절차입니다. 조직/워크스페이스에서 해당 기능과 권한을 사용할 수 있어야 합니다.

1. Platform의 터널 설정에서 터널을 생성하거나 재사용하고, 대상 Platform 조직과 ChatGPT 워크스페이스의 연결을 확인합니다. 생성에는 Tunnels Read + Manage, 실행에는 Read + Use 권한이 필요합니다.
2. 공식 안내의 다운로드 링크에서 지원되는 `tunnel-client`를 설치하고 `tunnel-client help quickstart`를 확인합니다. 런타임 키는 로컬 환경변수 `CONTROL_PLANE_API_KEY`로 설정하며 채팅·저장소·명령 인수에 키 값을 남기지 않습니다.
3. Pairper와 같은 사용자 계정 및 라이브러리 경로에서 stdio 프로필을 만듭니다.

```powershell
$env:PAIRPER_DATA_DIR = 'C:\Users\sreji\AppData\Local\Pairper\library'
tunnel-client init --sample sample_mcp_stdio_local --profile pairper --tunnel-id YOUR_TUNNEL_ID --mcp-command 'D:\project\18_pairper\.venv\Scripts\python.exe -m pairper.mcp.server'
tunnel-client doctor --profile pairper --explain
tunnel-client run --profile pairper
```

4. `tunnel-client run`은 연결하는 동안 유지합니다. ChatGPT의 사용자 지정 MCP 추가 화면에서 연결 방식 **Tunnel**과 해당 터널을 선택합니다. 실제 화면과 계정 권한에 따라 제공 여부가 달라질 수 있습니다.
5. 도구 발견 → 논문 목록 → 페이지 PNG가 모델에 보이는지 확인 → 실제 업데이트 요청 → 검증된 뷰 저장 → GUI 열람 순서로 확인합니다. 실패 단계는 그대로 보고하고 텍스트만 보고 시각 검증을 완료했다고 기록하지 않습니다.

Pairper 자체는 OpenAI API 키나 상시 AI 호출 엔진을 사용하지 않습니다. 위 키는 선택적 터널 클라이언트용입니다.
