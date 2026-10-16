# 로컬 UI 글꼴

영문·숫자는 Inter, 한국어는 Pretendard를 사용합니다. 폴백은 Malgun Gothic과 시스템 sans-serif입니다. 수식의 KaTeX 글꼴은 기존대로 유지합니다. 앱 실행 중 외부 폰트 서비스나 CDN에 접속하지 않습니다.

| 글꼴 | 고정 배포 버전과 원본 | 라이선스 파일 |
| --- | --- | --- |
| Inter Variable | [Inter v4.1](https://github.com/rsms/inter/tree/v4.1), `docs/font-files/InterVariable.woff2` | [전체 라이선스](../frontend/public/fonts/Inter-LICENSE.txt) |
| Pretendard Variable | [Pretendard v1.3.9](https://github.com/orioncactus/pretendard/tree/v1.3.9), `packages/pretendard/dist/web/variable/woff2/PretendardVariable.woff2` | [전체 라이선스](../frontend/public/fonts/Pretendard-LICENSE.txt) |

두 배포본의 라이선스는 SIL Open Font License 1.1입니다. 원본 폰트 파일을 수정하거나 이름을 바꾸지 않고 앱에 함께 배포하며, 저작권 고지와 전체 라이선스를 동봉합니다. 배포 허용 조건과 Reserved Font Name은 각 라이선스 원문을 따릅니다. 폰트를 단독 상품으로 판매하지 않습니다.

폰트 원본은 `frontend/src/assets/fonts/`, 라이선스 원문은 `frontend/public/fonts/`에 있습니다. 빌드 시 폰트는 `src/pairper/web/frontend/assets/`, 라이선스는 `src/pairper/web/frontend/fonts/`에 포함됩니다. `styles/tokens.css`의 `@font-face`는 이 로컬 자산을 사용합니다.
