# ezLab QA Monitor

이지랩(ezFinder / ezCapture / ezCam / ezMemo / ezZip / ezManager) 서비스 프로그램들의 크래시와 설치/삭제 이력을 실시간으로 감지하는 QA 전용 Windows 트레이 앱입니다.

## 기능

- Windows 이벤트 로그(Application/System)를 3초 주기로 폴링
- 크래시 감지: APPCRASH(1000), AppHang(1002), .NET Runtime(1026)
- 서비스 이상 감지: 비정상 종료(7031/7034), 시작 실패(7000), 시작/응답 시간 초과(7009/7011), 시작 중 멈춤(7022), 오류 종료(7023/7024)
- 설치/삭제/업데이트 이력 감지: 1033/1034/1035 (앱별 아코디언 카드뷰로 확인)
- **미실행 구간 백필**: 마지막으로 읽은 이벤트 로그 위치를 저장해두고, 모니터가 꺼져 있던 동안 발생한 크래시/설치 이벤트를 다음 실행 시 자동으로 따라잡아 기록 (합계 토스트 1회로 알림, 이벤트 로그 초기화 감지 시 안전하게 리셋)
- **크래시 덤프 자동 분석**: WER(Windows Error Reporting)이 캡처한 덤프를 ClrMD 기반 헬퍼(`DumpAnalyzer`)로 분석해 관리 코드 스택 트레이스를 크래시 이력에 자동 첨부 (AccessViolation류처럼 이벤트 로그만으로는 스택이 안 남는 크래시용)
- **실시간 응답 없음 감지 + 선제 덤프**: 이지랩 앱 창이 10초 이상 무응답이면 — Windows가 AppHang(1002)을 기록하기 전, 프로세스가 살아 있는 동안 — 전체 메모리 덤프를 미리 캡처하고 즉시 이력에 기록. 행(Hang)도 스택 분석이 가능해짐
- **연관 WER 진단 보고 자동 상관**: 크래시/행 전후 90초 내 같은 앱의 WER 1001 진단 보고(예: RADAR 메모리 폭주 감지)를 이력 상세에 자동 첨부 — "행 3초 전 메모리 급증" 같은 원인 힌트가 보고서에 포함됨
- **보고서+덤프 ZIP 내보내기**: 개발자 전달용으로 크래시 보고서 텍스트 + 연관 덤프(.dmp) + **덤프를 사람이 읽는 분석 리포트(.txt)** 를 ZIP 한 파일로 저장. 분석 리포트에는 전체 스레드 스택·예외·힙 통계·모듈이 담겨, 받는 사람이 `.dmp`를 디버거로 열지 않아도 크래시 지점을 바로 확인할 수 있음 (내장 `DumpAnalyzer`가 생성 — Visual Studio/dotnet-dump 불필요)
- **상세 분석 탭**: 이벤트 캡처 시점에 로캘 무관 파라미터(앱/모듈/버전/예외 코드/오프셋/경로/Report ID)를 구조화 저장하고, 주요 예외 코드(ACCESS_VIOLATION, 힙 손상, 스택 버퍼 오버런, .NET 예외, 어설션 패닉 등)별 원인 설명을 자동 표시
- 크래시 이력 앱/유형/기간(오늘·7일·30일) 필터와 텍스트 검색(앱·요약·로그 원문), 크래시·설치 이력 CSV 내보내기(Excel 한글 호환), 개별/전체 삭제
- 자기 감시: 모니터 자신의 크래시도 WER 덤프 등록 + 다음 실행 시 백필로 기록
- 감지 시 Windows 알림 센터 토스트(windows-toasts) — **알림을 클릭하면 크래시 이력 창이 바로 열림**, 발송 실패 시 트레이 풍선 알림으로 자동 폴백
- 감시 상태 표시: 트레이 툴팁에 마지막 폴링 시각 표시, 이벤트 로그 폴링이 연속 실패하면 "모니터링 오류" 토스트로 경고
- 크래시/설치 이력은 `%LOCALAPPDATA%\ezLab QA Monitor\`에 원자적 쓰기로 영구 저장(강제종료 시에도 손상 방지), 최근 500건만 보관
- 크래시 덤프는 `%ProgramData%\ezLab QA Monitor\Dumps\`에 최근 20개만 보관, 오래된 것은 자동 삭제

## 모니터링 대상

ezFinder, ezFinder Service, ezCapture, ezCam, ezMemo, ezZip, ezManager, ezManager Service (및 각 Updater), 그리고 모니터 자신

신규 앱 추가 시 `monitor.py`의 `EZLAB_APPS`(이름 매핑)와 `installer.iss`의 `WerAppNames`(덤프 등록)에 exe 이름을 추가한 뒤 재빌드/재배포가 필요합니다.

## 버전 관리

버전은 루트의 `VERSION` 파일이 단일 출처입니다. 앱 헤더 표시(`monitor.py`)와 인스톨러 버전(`installer.iss`)이 모두 이 파일을 읽으므로, 릴리스 시 이 파일만 올리면 됩니다.

## 개발 환경

```
pip install -r requirements.txt
python monitor.py
```

또는 `실행.bat`으로 바로 실행 (콘솔 창 없이 pythonw로 실행).

덤프 분석 기능(`DumpAnalyzer`) 빌드에는 [.NET 8 SDK](https://dotnet.microsoft.com/download)가 필요합니다.

## 빌드

```
build.bat
```

`DumpAnalyzer`(ClrMD 헬퍼)를 self-contained 단일 exe로 퍼블리시한 뒤, Nuitka로 `monitor.py`를 단일 exe로 빌드합니다. 빌드 결과물은 `dist\ezLabQAMonitor.exe`.

## 설치 파일 만들기

[Inno Setup](https://jrsoftware.org/isinfo.php) 6 설치 후 `installer.iss`를 컴파일하면 `dist\ezLabQAMonitor_Setup.exe`가 생성됩니다.

```
ISCC.exe installer.iss
```

설치 시 관리자 권한으로 다음을 자동 등록합니다:
- 자동시작 레지스트리(HKLM, 이 PC의 모든 사용자에게 적용)
- 모니터링 대상 앱 전체에 대한 WER LocalDumps(네이티브 64비트 레지스트리 뷰에 직접 등록 — 32비트 인스톨러가 기본 함수로 쓰면 WOW6432Node로 리다이렉트되어 64비트 대상 프로세스의 크래시 시 인식되지 않기 때문)

## 참고

- Nuitka `--onefile` 빌드는 실행할 때마다 임시 폴더에 압축을 풀기 때문에, 실행 파일 기준 상대 경로에는 영속 데이터를 저장하지 않습니다. 로그 데이터는 `%LOCALAPPDATA%\ezLab QA Monitor\`에 고정 저장됩니다.
- `dist/`, `crash_history.json`, `install_history.json`, `DumpAnalyzer.exe`(빌드 산출물), `*/bin`, `*/obj`는 로컬 전용이라 git에 포함하지 않습니다.
- `CrashTest/`는 덤프 분석 파이프라인을 실제 이지랩 제품 없이 검증하기 위한 합성 크래시 테스트용 프로그램입니다.
