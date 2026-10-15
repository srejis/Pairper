# 포함 리소스

KaTeX 0.16.22 배포본을 npm 공식 패키지 `https://registry.npmjs.org/katex/-/katex-0.16.22.tgz`에서 가져왔습니다. 라이선스는 `src/pairper/web/katex/LICENSE`에 포함되어 있습니다. 외부 CDN은 사용하지 않습니다.

TypeScript UI의 React/React DOM/KaTeX와 개발 도구 버전은 `frontend/package-lock.json`에 고정되어 있습니다. UI는 해당 npm KaTeX를 로컬 빌드에 포함합니다. Python HTML 렌더러의 이전 KaTeX 리소스도 비교/호환을 위해 유지합니다.

Python 패키지 버전은 `uv.lock`에 기록합니다. PySide6/Qt와 PyMuPDF 등 의존성의 라이선스는 향후 설치 프로그램을 배포하기 전에 해당 배포 방식에 맞게 확인해야 합니다.
