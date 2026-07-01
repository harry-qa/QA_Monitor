# ezLab QA Monitor

이지랩(ezFinder / ezCapture / ezCam / ezMemo / ezZip / ezManager) 서비스 프로그램들의 크래시와 설치/삭제 이력을 실시간으로 감지하는 QA 전용 Windows 트레이 앱입니다.

## 기능

- Windows 이벤트 로그(Application/System)를 3초 주기로 폴링
- 크래시 감지: APPCRASH(1000), AppHang(1002), .NET Runtime(1026), 서비스 비정상 종료(7031/7034)
- 설치/삭제/업데이트 이력 감지: 1033/1034/1035
- 감지 시 트레이 토스트 알림
- 크래시/설치 이력은 `%LOCALAPPDATA%\ezLab QA Monitor\`에 영구 저장 (앱 재시작해도 유지)

## 모니터링 대상

ezFinder, ezFinder Service, ezCapture, ezCam, ezMemo, ezZip, ezManager, ezManager Service (및 각 Updater)

## 개발 환경

```
pip install -r requirements.txt
python monitor.py
```

또는 `실행.bat`으로 바로 실행 (콘솔 창 없이 pythonw로 실행).

## 빌드

Nuitka로 단일 exe 빌드:

```
build.bat
```

빌드 결과물은 `dist\ezLabQAMonitor.exe`.

## 설치 파일 만들기

[Inno Setup](https://jrsoftware.org/isinfo.php) 6 설치 후 `installer.iss`를 컴파일하면 `dist\ezLabQAMonitor_Setup.exe`가 생성됩니다.

```
ISCC.exe installer.iss
```

## 참고

- Nuitka `--onefile` 빌드는 실행할 때마다 임시 폴더에 압축을 풀기 때문에, 실행 파일 기준 상대 경로에는 영속 데이터를 저장하지 않습니다. 로그 데이터는 `%LOCALAPPDATA%\ezLab QA Monitor\`에 고정 저장됩니다.
- `dist/`, `crash_history.json`, `install_history.json`은 로컬 전용 산출물/런타임 데이터로 git에 포함하지 않습니다.
