#!/usr/bin/env python3
"""
ezLab QA Monitor v1.0
이지랩 앱 실시간 크래시 / 로그 감지 도구
"""

import os
import sys
import json
import time
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional, List, Callable
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageFont, ImageTk

import ctypes
import winreg

import win32evtlog
import win32evtlogutil
import win32event
import win32api
import winerror

try:
    from win10toast import ToastNotifier
    _toaster = ToastNotifier()
    HAS_TOAST = True
except Exception:
    HAS_TOAST = False


# ── 경로 ─────────────────────────────────────────────────────────
# Nuitka onefile 빌드는 실행할 때마다 임시 폴더에 압축을 풀기 때문에
# __file__ 기준 경로는 재실행 시 유지되지 않는다. 로그처럼 영속시켜야
# 하는 데이터는 %LOCALAPPDATA% 아래 고정 경로에 저장한다.
BASE_DIR     = Path(__file__).parent
LOGO_PNG     = BASE_DIR / 'ezlab_logo.png'
LOGO_ICO     = BASE_DIR / 'ezlab.ico'

DATA_DIR     = Path(os.environ.get('LOCALAPPDATA', BASE_DIR)) / 'ezLab QA Monitor'
DATA_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = DATA_DIR / 'crash_history.json'

# WER(Windows Error Reporting)이 자동 저장하는 크래시 덤프(.dmp) 위치.
# 서비스 계정이 크래시해도 항상 같은 경로를 쓰도록 %LOCALAPPDATA%가 아니라
# 사용자와 무관한 %ProgramData%를 쓴다 (인스톨러가 이 경로로 LocalDumps를 등록함).
DUMP_DIR          = Path(os.environ.get('ProgramData', r'C:\ProgramData')) / 'ezLab QA Monitor' / 'Dumps'
DUMP_ANALYZER_EXE = BASE_DIR / 'DumpAnalyzer.exe'
MAX_DUMP_FILES    = 20  # 오래된 덤프는 자동 삭제, 최근 N개만 보관

APP_AUMID    = 'EzLab.QAMonitor'
APP_NAME     = 'ezLab QA Monitor'


def _register_aumid():
    """알림 발신자 이름을 'ezLab QA Monitor'로 표시하기 위해 AUMID 등록."""
    key_path = f'SOFTWARE\\Classes\\AppUserModelId\\{APP_AUMID}'
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            winreg.SetValueEx(k, 'DisplayName', 0, winreg.REG_SZ, APP_NAME)
            if LOGO_ICO.exists():
                winreg.SetValueEx(k, 'IconUri', 0, winreg.REG_SZ, str(LOGO_ICO))
    except Exception:
        pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_AUMID)
    except Exception:
        pass

# ── 이지랩 앱 정의 ────────────────────────────────────────────────
EZLAB_APPS = {
    'ezfinder.exe'          : 'ezFinder',
    'ezfinderservice.exe'   : 'ezFinder Service',
    'ezfinderservice'       : 'ezFinder Service',
    'ezfinder updator.exe'  : 'ezFinder Updater',
    'ezcapture.exe'         : 'ezCapture',
    'ezcapture updator.exe' : 'ezCapture Updater',
    'ezcam.exe'             : 'ezCam',
    'ezcam updator.exe'     : 'ezCam Updater',
    'ezmemo.exe'            : 'ezMemo',
    'ezmemo updator.exe'    : 'ezMemo Updater',
    'ezzip.exe'             : 'ezZip',
    'ezzip updator.exe'     : 'ezZip Updater',
    'ezmanager.exe'         : 'ezManager',
    'ezmanagerservice.exe'  : 'ezManager Service',
    'ezmanagerservice'      : 'ezManager Service',
    'ezmanager updator.exe' : 'ezManager Updater',
}

EZLAB_KEYWORDS = set(EZLAB_APPS.keys()) | {
    'ezfinder', 'ezcapture', 'ezcam', 'ezmemo',
    'ezzip', 'ezmanager', 'ezlab', 'mobsoft',
}

CRASH_EVENT_IDS = {
    1000: 'APPCRASH',
    1002: 'AppHang',
    1026: '.NET Runtime',
    7031: 'Service 비정상 종료',
    7034: 'Service 비정상 종료',
    7000: 'Service 시작 실패',
    7009: 'Service 시작 시간 초과',
    7011: 'Service 응답 시간 초과',
    7022: 'Service 시작 중 멈춤',
    7023: 'Service 오류 종료',
    7024: 'Service 오류 종료',
}

INSTALL_EVENT_IDS = {
    1033: '설치',
    1034: '삭제',
    1035: '업데이트',
}

EVENT_LOGS   = ['Application', 'System']
POLL_SECONDS = 3

# 이벤트 로그 폴링이 연속 실패할 때 "감시가 멈췄을 수 있다"고 알려주는 기준
POLL_FAIL_ALERT_THRESHOLD = 5    # 연속 실패 5회(약 15초) 시 최초 경고
POLL_FAIL_ALERT_REPEAT    = 100  # 이후에도 계속 실패하면 100회(약 5분)마다 재경고

# ── 라이트 테마 색상 ─────────────────────────────────────────────
BG      = '#F4F6F8'   # 배경
SURFACE = '#FFFFFF'   # 표면
PANEL   = '#FFFFFF'   # 패널
CARD    = '#FFFFFF'   # 카드
OVERLAY = '#EBF4FF'   # 선택 (연파랑)
BORDER  = '#E2E8F0'   # 구분선

FG      = '#1A202C'   # 기본 텍스트
MUTED   = '#A0AEC0'   # 보조 텍스트
SUBTLE  = '#718096'   # 중간 텍스트

RED     = '#C53030'   # 크래시/오류
ORANGE  = '#C05621'   # 경고
YELLOW  = '#B7791F'   # 주의
GREEN   = '#276749'   # 정상
BLUE    = '#2B6CB0'   # 정보
TEAL    = '#2C7A7B'   # 서비스
MAUVE   = '#553C9A'   # 강조
CYAN    = '#2A4365'   # 스택 트레이스

RED_BG  = '#FFF5F5'   # 크래시 카드 배경 틴트
BLUE_BG = '#EBF8FF'   # 서비스 카드 배경 틴트


# ── 데이터 모델 & 영속성 ──────────────────────────────────────────
@dataclass
class CrashEvent:
    timestamp:  datetime
    app_name:   str
    process:    str
    error_type: str
    summary:    str
    detail:     str
    level:      str = 'Error'


@dataclass
class InstallEvent:
    timestamp: datetime
    app_name:  str
    version:   str
    action:    str   # '설치' | '삭제'


def _to_dict(ev: CrashEvent) -> dict:
    d = asdict(ev)
    d['timestamp'] = ev.timestamp.strftime('%Y-%m-%d %H:%M:%S')
    return d


def _from_dict(d: dict) -> CrashEvent:
    d = dict(d)
    d['timestamp'] = datetime.strptime(d['timestamp'], '%Y-%m-%d %H:%M:%S')
    return CrashEvent(**d)


def _install_to_dict(ev: InstallEvent) -> dict:
    d = asdict(ev)
    d['timestamp'] = ev.timestamp.strftime('%Y-%m-%d %H:%M:%S')
    return d


def _install_from_dict(d: dict) -> InstallEvent:
    d = dict(d)
    d['timestamp'] = datetime.strptime(d['timestamp'], '%Y-%m-%d %H:%M:%S')
    return InstallEvent(**d)


INSTALL_HISTORY_FILE = DATA_DIR / 'install_history.json'


MAX_HISTORY_ENTRIES = 500  # 오래된 항목은 자동으로 정리하고 최근 N개만 보관


def _atomic_write_json(path: Path, data) -> None:
    # 쓰는 도중 강제 종료/크래시가 나도 기존 파일이 손상되지 않도록
    # 임시 파일에 먼저 쓰고 os.replace()로 통째로 교체한다.
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_history() -> List[CrashEvent]:
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            return [_from_dict(item) for item in json.load(f)]
    except Exception:
        return []


def save_history(history: List[CrashEvent]) -> None:
    if len(history) > MAX_HISTORY_ENTRIES:
        history = history[-MAX_HISTORY_ENTRIES:]
    try:
        _atomic_write_json(HISTORY_FILE, [_to_dict(e) for e in history])
    except Exception:
        pass


def append_to_history(ev: CrashEvent) -> None:
    history = load_history()
    history.append(ev)
    save_history(history)


def load_install_history() -> List[InstallEvent]:
    if not INSTALL_HISTORY_FILE.exists():
        return []
    try:
        with open(INSTALL_HISTORY_FILE, 'r', encoding='utf-8') as f:
            return [_install_from_dict(item) for item in json.load(f)]
    except Exception:
        return []


def save_install_history(history: List[InstallEvent]) -> None:
    if len(history) > MAX_HISTORY_ENTRIES:
        history = history[-MAX_HISTORY_ENTRIES:]
    try:
        _atomic_write_json(INSTALL_HISTORY_FILE, [_install_to_dict(e) for e in history])
    except Exception:
        pass


def append_install_history(ev: InstallEvent) -> None:
    history = load_install_history()
    history.append(ev)
    save_install_history(history)


# ── 이벤트 로그 감시 ─────────────────────────────────────────────
class EventLogWatcher:
    def __init__(self, on_event: Callable[[CrashEvent], None],
                 on_install: Callable[[InstallEvent], None] = None,
                 on_poll: Callable[[bool], None] = None):
        self._on_event   = on_event
        self._on_install = on_install
        self._on_poll    = on_poll
        self._stop       = threading.Event()
        self._last_record: dict = {}  # log_name -> last processed RecordNumber

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name='LogWatcher').start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(POLL_SECONDS):
            ok = True
            try:
                self._poll()
            except Exception:
                ok = False
            if self._on_poll:
                try:
                    self._on_poll(ok)
                except Exception:
                    pass

    def _poll(self):
        for log_name in EVENT_LOGS:
            try:
                handle = win32evtlog.OpenEventLog(None, log_name)
                flags  = (win32evtlog.EVENTLOG_BACKWARDS_READ |
                          win32evtlog.EVENTLOG_SEQUENTIAL_READ)

                last_rec = self._last_record.get(log_name, None)

                # 첫 폴: 현재 최대 RecordNumber만 기록하고 이벤트는 처리 안 함
                if last_rec is None:
                    chunk = win32evtlog.ReadEventLog(handle, flags, 0)
                    if chunk:
                        self._last_record[log_name] = max(e.RecordNumber for e in chunk)
                    else:
                        self._last_record[log_name] = 0
                    win32evtlog.CloseEventLog(handle)
                    continue

                new_max = last_rec
                to_process = []
                done = False
                while not done:
                    chunk = win32evtlog.ReadEventLog(handle, flags, 0)
                    if not chunk:
                        break
                    for ev in chunk:
                        if ev.RecordNumber <= last_rec:
                            done = True
                            break
                        new_max = max(new_max, ev.RecordNumber)
                        eid = ev.EventID & 0xFFFF
                        if eid in CRASH_EVENT_IDS or eid in INSTALL_EVENT_IDS:
                            to_process.append(ev)

                self._last_record[log_name] = new_max
                win32evtlog.CloseEventLog(handle)

                # 오래된 것부터 순서대로 처리
                for ev in reversed(to_process):
                    self._process(log_name, ev)

            except Exception:
                pass

    def _process(self, log_name: str, ev):
        eid = ev.EventID & 0xFFFF
        ts  = self._to_dt(ev.TimeGenerated)

        # ── 설치/삭제 이벤트 ──
        if eid in INSTALL_EVENT_IDS and ev.SourceName == 'MsiInstaller':
            inserts = list(ev.StringInserts or [])
            install = self._parse_install(ts, eid, inserts)
            if install and self._on_install:
                self._on_install(install)
            return

        # ── 크래시 이벤트 ──
        try:
            raw = win32evtlogutil.SafeFormatMessage(ev, ev.SourceName)
            if raw.startswith('The description for Event ID'):
                raw = ''
        except Exception:
            raw = ''

        inserts = list(ev.StringInserts or [])
        msg     = raw if raw else self._format_inserts(eid, inserts)
        detail  = self._build_detail(log_name, eid, ev.SourceName, ts, msg, inserts, not raw)

        if not self._is_ezlab(detail + ev.SourceName, ev.SourceName):
            return
        crash = self._parse(ts, eid, msg, detail, inserts)
        if crash:
            self._on_event(crash)

    def _parse_install(self, ts: datetime, eid: int, inserts: list) -> Optional[InstallEvent]:
        # MsiInstaller 1033/1034/1035 inserts: [제품명, 버전, 언어, 제조사, 결과코드]
        app_name     = self._ins(inserts, 0)
        version      = self._ins(inserts, 1)
        manufacturer = self._ins(inserts, 3)

        # MobSoft 제품만 기록
        if not app_name:
            return None
        mfr_lower = manufacturer.lower()
        app_lower = app_name.lower()
        if 'mobsoft' not in mfr_lower and not any(
            kw in app_lower for kw in ('ezfinder','ezcapture','ezcam','ezmemo','ezzip','ezmanager')
        ):
            return None

        if eid == 1035:
            action = '업데이트'
        elif eid == 1033:
            # 이전에 다른 버전이 설치돼 있었으면 업데이트로 분류
            action = '설치'
            history = load_install_history()
            prev = next((e for e in reversed(history)
                         if e.app_name == app_name and e.action in ('설치', '업데이트')), None)
            if prev and prev.version != version:
                action = '업데이트'
        else:
            action = INSTALL_EVENT_IDS.get(eid, '알 수 없음')

        return InstallEvent(ts, app_name, version, action)

    @staticmethod
    def _to_dt(t) -> datetime:
        try:
            return datetime(t.year, t.month, t.day, t.hour, t.minute, t.second)
        except Exception:
            return datetime.now()

    @staticmethod
    def _format_inserts(eid: int, inserts: list) -> str:
        labels = {
            1000: ['앱 이름', '버전', '타임스탬프', '모듈 이름', '모듈 버전',
                   '모듈 타임스탬프', '예외 코드', '오프셋', '프로세스 ID',
                   '시작 시간', '앱 경로', '모듈 경로', 'Report ID'],
            1002: ['앱 이름', '버전', '타임스탬프', '대기 시간(초)',
                   '앱 경로', 'Report ID'],
            7031: ['서비스 이름', '종료 횟수', '복구 동작'],
            7034: ['서비스 이름', '종료 횟수'],
            7000: ['서비스 이름', '오류 내용'],
            7009: ['시간 제한(ms)', '서비스 이름'],
            7011: ['시간 제한(ms)', '서비스 이름'],
            7022: ['서비스 이름'],
            7023: ['서비스 이름', '오류 내용'],
            7024: ['서비스 이름', '서비스 특정 오류'],
        }
        keys  = labels.get(eid, [])
        lines = []
        for i, val in enumerate(inserts):
            if not val:
                continue
            label = keys[i] if i < len(keys) else f'파라미터[{i}]'
            lines.append(f'{label:<18}: {val}')
        return '\n'.join(lines) if lines else '(파라미터 없음)'

    @staticmethod
    def _build_detail(log_name, eid, source, ts, msg, inserts, from_inserts) -> str:
        lines = [
            f'로그 채널   : {log_name}',
            f'이벤트 ID   : {eid}  ({CRASH_EVENT_IDS.get(eid, "")})',
            f'소스        : {source}',
            f'발생 시각   : {ts.strftime("%Y-%m-%d %H:%M:%S")}',
            '─' * 48,
            '',
            '[파라미터]' if from_inserts else '[이벤트 메시지]',
            msg.strip(),
        ]
        return '\n'.join(lines)

    def _is_ezlab(self, msg: str, source: str) -> bool:
        m, s = msg.lower(), source.lower()
        return any(kw in m or kw in s for kw in EZLAB_KEYWORDS)

    def _resolve(self, proc: str) -> str:
        return EZLAB_APPS.get(proc.lower(), proc)

    def _parse(self, ts, eid, msg, detail, inserts: list) -> Optional[CrashEvent]:
        if eid == 1026:               return self._dotnet(ts, msg, detail, inserts)
        if eid == 1000:               return self._appcrash(ts, msg, detail, inserts)
        if eid == 1002:               return self._hang(ts, msg, detail, inserts)
        if eid in (7031, 7034):       return self._service(ts, msg, detail, inserts)
        if eid in (7000, 7023, 7024): return self._service_error(ts, eid, msg, detail, inserts)
        if eid in (7009, 7011):       return self._service_timeout(ts, eid, msg, detail, inserts)
        if eid == 7022:               return self._service_hang(ts, msg, detail, inserts)
        return None

    @staticmethod
    def _ins(inserts: list, idx: int) -> str:
        try:
            return (inserts[idx] or '').strip()
        except IndexError:
            return ''

    def _dotnet(self, ts, msg, detail, inserts) -> Optional[CrashEvent]:
        # inserts[0] = 전체 메시지 텍스트 (1026은 단일 파라미터)
        proc = exc = ''
        for line in msg.splitlines():
            s = line.strip()
            if s.startswith('Application:') and not proc:
                proc = s.split(':', 1)[1].strip()
            elif 'Exception Info:' in s and not exc:
                part = s.split('Exception Info:', 1)[1].strip()
                exc  = part.split(':')[0].strip()
        if not proc:
            # 인서트에서 직접 파일명 추출 시도
            for ins in inserts:
                if ins and ins.lower().endswith('.exe'):
                    proc = ins; break
        if not proc:
            return None
        return CrashEvent(ts, self._resolve(proc), proc,
                          '.NET 비정상 종료', exc or '알 수 없는 예외', detail)

    def _appcrash(self, ts, msg, detail, inserts) -> Optional[CrashEvent]:
        # inserts[0]=앱명, [3]=모듈명, [6]=예외코드, [10]=앱경로, [11]=모듈경로
        proc   = self._ins(inserts, 0)
        module = self._ins(inserts, 3)
        code   = self._ins(inserts, 6)

        # SafeFormatMessage가 성공한 경우 텍스트에서도 추출 (더 정확할 수 있음)
        if not proc:
            for line in msg.splitlines():
                if 'Faulting application name:' in line:
                    proc = line.split(',')[0].split(':', 1)[-1].strip(); break
        if not module:
            for line in msg.splitlines():
                if 'Faulting module name:' in line:
                    module = line.split(',')[0].split(':', 1)[-1].strip(); break
        if not code:
            for line in msg.splitlines():
                if 'Exception code:' in line:
                    code = line.split(':', 1)[-1].strip(); break

        if not proc:
            return None

        exc_label = {
            '0xc0000005': '메모리 접근 위반 (ACCESS_VIOLATION)',
            '0xc0000409': '스택 오버플로우',
            '0x40000015': 'Fatal App Exit',
            '0xc0000374': '힙 손상 (Heap Corruption)',
            '0xe0434352': '.NET 예외',
        }.get(code.lower(), code)

        return CrashEvent(ts, self._resolve(proc), proc,
                          '앱 오류 (APPCRASH)',
                          f'충돌 모듈: {module}  |  {exc_label}', detail)

    def _hang(self, ts, msg, detail, inserts) -> Optional[CrashEvent]:
        # inserts[0]=앱명, [4]=대기 시간(초), [5]=앱 경로
        proc      = self._ins(inserts, 0)
        wait_sec  = self._ins(inserts, 4)

        if not proc:
            # 메시지 텍스트에서 추출
            for line in msg.splitlines():
                if 'program' in line.lower():
                    words = line.split()
                    for i, w in enumerate(words):
                        if w.lower() == 'program' and i + 1 < len(words):
                            proc = words[i + 1]; break
                    break

        wait_label = f'{wait_sec}초 동안 응답 없음' if wait_sec else 'UI 스레드 응답 없음'
        return CrashEvent(ts, self._resolve(proc), proc,
                          '응답 없음 (Hang)',
                          f'{wait_label} — Windows에 의해 강제 종료됨', detail)

    def _service(self, ts, msg, detail, inserts) -> Optional[CrashEvent]:
        # inserts[0]=서비스 이름, [1]=종료 횟수
        svc = self._ins(inserts, 0)

        if not svc:
            for line in msg.splitlines():
                l = line.lower()
                if 'service' in l and ('terminated' in l or 'stopped' in l):
                    words = line.split()
                    for i, w in enumerate(words):
                        if w.lower() == 'service' and i > 0:
                            svc = words[i - 1]; break
                    if svc: break
        if not svc:
            return None
        return CrashEvent(ts, self._resolve(svc),  svc,
                          '서비스 비정상 종료',
                          f'{svc} 서비스가 예기치 않게 종료됨', detail)

    def _service_error(self, ts, eid, msg, detail, inserts) -> Optional[CrashEvent]:
        # 7000(시작 실패)/7023/7024(오류 종료) inserts[0]=서비스 이름, [1]=오류 내용
        svc = self._ins(inserts, 0)
        err = self._ins(inserts, 1)
        if not svc:
            return None
        label = CRASH_EVENT_IDS[eid]
        return CrashEvent(ts, self._resolve(svc), svc, label,
                          f'{svc} — {err}' if err else f'{svc} {label}', detail)

    def _service_timeout(self, ts, eid, msg, detail, inserts) -> Optional[CrashEvent]:
        # 7009(시작 시간 초과)/7011(응답 시간 초과) inserts[0]=시간 제한(ms), [1]=서비스 이름
        timeout = self._ins(inserts, 0)
        svc     = self._ins(inserts, 1)
        if not svc:
            return None
        label = CRASH_EVENT_IDS[eid]
        return CrashEvent(ts, self._resolve(svc), svc, label,
                          f'{timeout}ms 동안 응답 없음' if timeout else f'{svc} {label}', detail)

    def _service_hang(self, ts, msg, detail, inserts) -> Optional[CrashEvent]:
        # 7022 inserts[0]=서비스 이름
        svc = self._ins(inserts, 0)
        if not svc:
            return None
        return CrashEvent(ts, self._resolve(svc), svc, '서비스 시작 중 멈춤',
                          f'{svc} 서비스가 시작 중 응답 없음', detail)


# ── 크래시 덤프 감시 (ClrMD 기반 관리 코드 스택 트레이스 자동 분석) ──
class DumpWatcher:
    """WER LocalDumps가 떨어뜨린 .dmp 파일을 감시하다가 DumpAnalyzer.exe로
    관리 코드 스택 트레이스를 뽑아 콜백으로 전달한다. 오래된 덤프는 자동 정리."""

    def __init__(self, on_stack_trace: Callable[[str, datetime, str], None]):
        self._on_stack_trace = on_stack_trace
        self._seen: set = set()
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name='DumpWatcher').start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(POLL_SECONDS):
            try:
                self._poll()
            except Exception:
                pass

    def _poll(self):
        if not DUMP_DIR.exists():
            return
        dumps = sorted(DUMP_DIR.glob('*.dmp'), key=lambda p: p.stat().st_mtime)

        for dmp in dumps:
            if dmp.name in self._seen:
                continue
            self._seen.add(dmp.name)
            self._analyze(dmp)

        # 보관 개수 제한: 오래된 것부터 정리
        dumps = sorted(DUMP_DIR.glob('*.dmp'), key=lambda p: p.stat().st_mtime)
        for old in dumps[:-MAX_DUMP_FILES] if len(dumps) > MAX_DUMP_FILES else []:
            try:
                old.unlink()
            except Exception:
                pass

    def _analyze(self, dmp_path: Path):
        if not DUMP_ANALYZER_EXE.exists():
            return
        # WER 기본 파일명 형식: <프로세스exe>.<PID>.dmp
        proc_name = dmp_path.stem.rsplit('.', 1)[0]
        try:
            result = subprocess.run(
                [str(DUMP_ANALYZER_EXE), str(dmp_path)],
                capture_output=True, text=True, timeout=90,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            text = (result.stdout or '').strip()
        except Exception:
            return
        if not text or text.startswith(('NO_CLR_RUNTIME_FOUND', 'NO_MANAGED_THREAD_FOUND', 'ANALYZER_ERROR')):
            return
        ts = datetime.fromtimestamp(dmp_path.stat().st_mtime)
        self._on_stack_trace(proc_name, ts, text)


# ── 유틸 ─────────────────────────────────────────────────────────
def _make_report(ev: CrashEvent) -> str:
    sep = '═' * 52
    return (
        f'[이지랩 QA 크래시 리포트]\n{sep}\n'
        f'발생 시각   : {ev.timestamp.strftime("%Y-%m-%d %H:%M:%S")}\n'
        f'앱          : {ev.app_name}  ({ev.process})\n'
        f'유형        : {ev.error_type}\n'
        f'{sep}\n\n'
        f'요약\n{ev.summary}\n\n'
        f'원문 로그\n{"─"*52}\n{ev.detail}'
    )


# ── 이력 창 ──────────────────────────────────────────────────────
class HistoryWindow:
    def __init__(self, history: List[CrashEvent],
                 install_history: List[InstallEvent] = None,
                 on_open: Callable = None,
                 on_delete_crash: Callable = None,
                 on_delete_install: Callable = None):
        self._history         = history
        self._install_history = list(install_history or [])
        self._on_open         = on_open
        self._on_delete_crash   = on_delete_crash
        self._on_delete_install = on_delete_install
        self._selected: Optional[CrashEvent] = None
        self._card_frames: List[tk.Frame] = []
        self._card_tints:  List[str] = []
        self._card_bars:   List[tk.Frame] = []
        self._root: Optional[tk.Tk] = None
        self._count_var: Optional[tk.StringVar] = None
        self._count_label: Optional[tk.Label] = None
        self._install_frame: Optional[tk.Frame] = None  # 설치 이력 테이블 컨테이너

    def push_event(self, ev: CrashEvent):
        """스레드 안전: 새 크래시를 UI 메인 스레드에서 반영."""
        if self._root and self._root.winfo_exists():
            self._root.after(0, lambda e=ev: self._on_new_event(e))

    def push_install(self, ev: InstallEvent):
        """스레드 안전: 새 설치/삭제 이벤트를 UI 메인 스레드에서 반영."""
        if self._root and self._root.winfo_exists():
            self._root.after(0, lambda e=ev: self._on_new_install(e))

    def _on_new_event(self, ev: CrashEvent):
        self._indexed.insert(0, ev)
        self._rebuild_cards()
        self._update_count()
        self._select(0)

    def _on_new_install(self, ev: InstallEvent):
        self._install_history.insert(0, ev)
        if self._install_frame:
            self._rebuild_install_table()

    def _rebuild_cards(self):
        for w in self._card_container.winfo_children():
            w.destroy()
        self._card_frames.clear()
        self._card_tints.clear()
        self._card_bars.clear()
        self._build_date_groups(self._card_container)

    def _build_date_groups(self, parent: tk.Frame):
        from collections import defaultdict
        groups: dict = defaultdict(list)
        for i, ev in enumerate(self._indexed):
            groups[ev.timestamp.date()].append((i, ev))

        day_names = ['월', '화', '수', '목', '금', '토', '일']

        for date in sorted(groups.keys(), reverse=True):
            items  = groups[date]
            count  = len(items)
            day    = day_names[date.weekday()]
            label  = f'{date.strftime("%Y.%m.%d")} ({day})  {count}건'

            # 날짜 그룹 컨테이너
            grp = tk.Frame(parent, bg=BG)
            grp.pack(fill=tk.X)

            cards_frame = tk.Frame(parent, bg=BG)
            cards_frame.pack(fill=tk.X)

            expanded = [True]

            def _toggle(cf=cards_frame, btn_ref=None, state=expanded,
                        lbl_open=f'▾  {label}', lbl_close=f'▸  {label}'):
                state[0] = not state[0]
                if state[0]:
                    cf.pack(fill=tk.X)
                    btn_ref.config(text=lbl_open)
                else:
                    cf.pack_forget()
                    btn_ref.config(text=lbl_close)

            hdr_btn = tk.Button(
                grp, text=f'▾  {label}',
                font=('Segoe UI', 8, 'bold'),
                bg=BG, fg=SUBTLE,
                relief=tk.FLAT, bd=0, anchor=tk.W,
                padx=16, pady=9, cursor='hand2',
                activebackground=BORDER, activeforeground=FG)
            hdr_btn.config(command=lambda b=hdr_btn, fn=_toggle: fn(btn_ref=b))
            hdr_btn.pack(fill=tk.X)
            tk.Frame(grp, bg=BORDER, height=1).pack(fill=tk.X)

            for idx, ev in items:
                self._add_card(cards_frame, ev, idx)

    def _update_count(self):
        n = len(self._indexed)
        if self._count_var:
            self._count_var.set(f'  {n}건  ')
        if self._count_label and n > 0:
            try:
                self._count_label.pack(side=tk.RIGHT, padx=(10, 0))
            except Exception:
                pass

    def show(self):
        root = tk.Tk()
        self._root = root
        root.title('ezLab QA Monitor')
        root.geometry('1320x780')
        root.configure(bg=BG)
        root.minsize(900, 560)

        if LOGO_ICO.exists():
            root.iconbitmap(str(LOGO_ICO))

        if self._on_open:
            root.after(150, self._on_open)

        self._setup_styles()
        self._build_header(root)
        self._build_tab_switcher(root)
        self._crash_frame  = tk.Frame(root, bg=BG)
        self._install_view = tk.Frame(root, bg=BG)
        self._build_body(self._crash_frame)
        self._build_install_view(self._install_view)
        self._crash_frame.pack(fill=tk.BOTH, expand=True)
        root.bind_all('<MouseWheel>', self._on_mousewheel)
        root.mainloop()

    # ── 상단 탭 스위처 ──
    def _build_tab_switcher(self, parent: tk.Tk):
        bar = tk.Frame(parent, bg=SURFACE, height=36)
        bar.pack(fill=tk.X)
        bar.pack_propagate(False)

        self._tab_btns = {}

        def switch(name):
            if name == 'crash':
                self._crash_frame.pack(fill=tk.BOTH, expand=True)
                self._install_view.pack_forget()
            else:
                self._install_view.pack(fill=tk.BOTH, expand=True)
                self._crash_frame.pack_forget()
            for n, b in self._tab_btns.items():
                b.config(bg=SURFACE if n == name else BG,
                         fg=BLUE if n == name else MUTED,
                         font=('Segoe UI', 9, 'bold' if n == name else 'normal'))

        for key, label in [('crash', '  크래시 이력  '), ('install', '  설치 / 삭제 이력  ')]:
            btn = tk.Button(bar, text=label,
                            font=('Segoe UI', 9, 'bold' if key == 'crash' else 'normal'),
                            bg=SURFACE if key == 'crash' else BG,
                            fg=BLUE if key == 'crash' else MUTED,
                            relief=tk.FLAT, bd=0, padx=8,
                            activebackground=SURFACE, activeforeground=BLUE,
                            cursor='hand2',
                            command=lambda k=key: switch(k))
            btn.pack(side=tk.LEFT, fill=tk.Y)
            self._tab_btns[key] = btn

        tk.Frame(parent, bg=BORDER, height=1).pack(fill=tk.X)

    # ── 설치 이력 뷰 ──
    def _build_install_view(self, parent: tk.Frame):
        # 헤더
        hdr = tk.Frame(parent, bg=SURFACE, pady=10, padx=16)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text='앱별 설치 / 삭제 이력',
                 font=('Segoe UI', 10, 'bold'), bg=SURFACE, fg=FG).pack(side=tk.LEFT)
        tk.Label(hdr, text='MobSoft 제품만 표시됩니다',
                 font=('Segoe UI', 8), bg=SURFACE, fg=MUTED).pack(side=tk.LEFT, padx=12)
        clear_all_lbl = tk.Label(hdr, text='전체 삭제', font=('Segoe UI', 8),
                                  bg=SURFACE, fg=RED, cursor='hand2')
        clear_all_lbl.pack(side=tk.RIGHT)
        clear_all_lbl.bind('<Button-1>', lambda e: self._clear_all_install())
        tk.Frame(parent, bg=BORDER, height=1).pack(fill=tk.X)

        # 스크롤 영역
        canvas = tk.Canvas(parent, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)

        self._install_frame = tk.Frame(canvas, bg=BG)
        _cid = canvas.create_window((0, 0), window=self._install_frame, anchor='nw')

        def _on_resize(e):
            canvas.configure(scrollregion=canvas.bbox('all'))
            canvas.itemconfig(_cid, width=canvas.winfo_width())

        self._install_canvas = canvas
        self._install_frame.bind('<Configure>', _on_resize)
        canvas.bind('<Configure>', lambda e: canvas.itemconfig(_cid, width=e.width))

        sb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._rebuild_install_table()

    def _rebuild_install_table(self):
        if not self._install_frame:
            return
        for w in self._install_frame.winfo_children():
            w.destroy()

        # 앱별로 그룹핑 (이름 기준)
        grouped: dict = {}
        for ev in sorted(self._install_history, key=lambda e: e.timestamp, reverse=True):
            grouped.setdefault(ev.app_name, []).append(ev)

        if not grouped:
            tk.Label(self._install_frame, text='기록된 설치/삭제 이력이 없습니다.',
                     font=('Segoe UI', 10), bg=BG, fg=MUTED
                     ).pack(pady=40)
            return

        for app_name, events in sorted(grouped.items()):
            latest = events[0]

            # 카드 컨테이너 (헤더 + 접히는 행 영역)
            card = tk.Frame(self._install_frame, bg=PANEL)
            card.pack(fill=tk.X, padx=16, pady=(12, 0))

            # 나중에 pack/pack_forget을 반복해도 항상 이 카드 바로 아래에
            # 위치하도록 after=card로 고정한다 (pack은 기본적으로 맨 뒤에
            # 추가되므로, 지정 없이 다시 pack하면 목록 맨 아래로 밀려남).
            rows_frame = tk.Frame(self._install_frame, bg=BG)
            rows_frame.pack(fill=tk.X, padx=16, after=card)
            rows_frame.pack_forget()
            expanded = [False]

            def _toggle(rf=rows_frame, btn_ref=None, state=expanded, after_widget=card):
                state[0] = not state[0]
                if state[0]:
                    rf.pack(fill=tk.X, padx=16, after=after_widget)
                    btn_ref.config(text='▾')
                else:
                    rf.pack_forget()
                    btn_ref.config(text='▸')

            hdr = tk.Frame(card, bg=PANEL, cursor='hand2')
            hdr.pack(fill=tk.X)

            toggle_btn = tk.Label(hdr, text='▸', font=('Segoe UI', 9, 'bold'),
                                   bg=PANEL, fg=SUBTLE, padx=6)
            toggle_btn.pack(side=tk.LEFT, padx=(8, 0), pady=8)

            tk.Label(hdr, text=app_name,
                     font=('Segoe UI', 10, 'bold'), bg=PANEL, fg=FG,
                     pady=8, anchor=tk.W
                     ).pack(side=tk.LEFT)

            tk.Label(hdr, text=f'{len(events)}건',
                     font=('Segoe UI', 8), bg=PANEL, fg=MUTED
                     ).pack(side=tk.LEFT, padx=8)

            latest_color = GREEN if latest.action == '설치' else YELLOW if latest.action == '업데이트' else RED
            tk.Label(hdr, text=f'최근: {latest.action} · {latest.version}',
                     font=('Segoe UI', 8), bg=PANEL, fg=latest_color
                     ).pack(side=tk.RIGHT, padx=14)

            for w in (hdr, toggle_btn):
                w.bind('<Button-1>', lambda e, b=toggle_btn, fn=_toggle: fn(btn_ref=b))
            for child in hdr.winfo_children():
                child.bind('<Button-1>', lambda e, b=toggle_btn, fn=_toggle: fn(btn_ref=b))

            # 이벤트 행 (기본은 접혀있음, 헤더 클릭 시 펼침)
            for ev in events:
                row = tk.Frame(rows_frame, bg=CARD)
                row.pack(fill=tk.X)
                tk.Frame(rows_frame, bg=BORDER, height=1).pack(fill=tk.X)

                action_color = GREEN if ev.action == '설치' else YELLOW if ev.action == '업데이트' else RED
                # 액션 뱃지
                tk.Label(row,
                         text=f'  {ev.action}  ',
                         font=('Segoe UI', 8, 'bold'),
                         bg=action_color, fg=SURFACE,
                         padx=2
                         ).pack(side=tk.LEFT, padx=(14, 10), pady=8)

                tk.Label(row, text=ev.version,
                         font=('Consolas', 9), bg=CARD, fg=CYAN,
                         width=12, anchor=tk.W
                         ).pack(side=tk.LEFT)

                tk.Label(row, text=ev.timestamp.strftime('%Y-%m-%d  %H:%M:%S'),
                         font=('Segoe UI', 9), bg=CARD, fg=MUTED
                         ).pack(side=tk.RIGHT, padx=14)

                del_lbl = tk.Label(row, text='✕', font=('Segoe UI', 9, 'bold'),
                                    bg=CARD, fg=MUTED, cursor='hand2')
                del_lbl.pack(side=tk.RIGHT, padx=(0, 4))
                del_lbl.bind('<Button-1>', lambda e, ev=ev: self._delete_install(ev))

            tk.Frame(self._install_frame, bg=BORDER, height=1).pack(fill=tk.X, padx=16, pady=(0, 4))

    def _delete_install(self, ev: InstallEvent):
        if not messagebox.askyesno('삭제 확인',
                                    f'{ev.app_name}  {ev.action} · {ev.version} 기록을 삭제할까요?'):
            return
        self._install_history.remove(ev)
        if self._on_delete_install:
            self._on_delete_install([ev])
        self._rebuild_install_table()

    def _clear_all_install(self):
        if not self._install_history:
            return
        if not messagebox.askyesno(
                '전체 삭제 확인',
                f'설치/삭제 이력 전체 {len(self._install_history)}건을 삭제할까요?\n이 작업은 되돌릴 수 없습니다.'):
            return
        evs = list(self._install_history)
        self._install_history.clear()
        if self._on_delete_install:
            self._on_delete_install(evs)
        self._rebuild_install_table()

    # ── 스타일 ──
    def _setup_styles(self):
        s = ttk.Style()
        s.theme_use('clam')
        s.configure('Vertical.TScrollbar',
                    background='#CBD5E0', troughcolor=BG,
                    borderwidth=0, arrowsize=0, width=6, relief=tk.FLAT)
        s.map('Vertical.TScrollbar',
              background=[('active', BLUE), ('pressed', BLUE)])
        s.configure('QA.TSeparator', background=BORDER)

    # ── 헤더 ──
    def _build_header(self, parent: tk.Tk):
        hdr = tk.Frame(parent, bg=SURFACE, height=60)
        hdr.pack(fill=tk.X)
        hdr.pack_propagate(False)

        # 로고
        if LOGO_PNG.exists():
            try:
                logo = Image.open(LOGO_PNG).convert('RGBA').resize((30, 30), Image.LANCZOS)
                self._logo_photo = ImageTk.PhotoImage(logo)
                tk.Label(hdr, image=self._logo_photo, bg=SURFACE).pack(
                    side=tk.LEFT, padx=(20, 8), pady=15)
            except Exception:
                pass

        title_frame = tk.Frame(hdr, bg=SURFACE)
        title_frame.pack(side=tk.LEFT, pady=15)
        tk.Label(title_frame, text='ezLab QA Monitor',
                 font=('Segoe UI', 13, 'bold'), bg=SURFACE, fg=FG
                 ).pack(anchor=tk.W)

        # 오른쪽 상태
        right = tk.Frame(hdr, bg=SURFACE)
        right.pack(side=tk.RIGHT, padx=20, pady=15)

        n = len(self._history)
        self._count_var = tk.StringVar(value=f'  {n}건  ')
        self._count_label = tk.Label(right, textvariable=self._count_var,
                                     font=('Segoe UI', 8, 'bold'),
                                     bg=RED, fg='#FFFFFF', padx=4, pady=2)
        if n > 0:
            self._count_label.pack(side=tk.RIGHT, padx=(10, 0))

        status_frame = tk.Frame(right, bg=SURFACE)
        status_frame.pack(side=tk.RIGHT)
        dot_canvas = tk.Canvas(status_frame, width=8, height=8, bg=SURFACE, highlightthickness=0)
        dot_canvas.pack(side=tk.LEFT, padx=(0, 4))
        dot_canvas.create_oval(1, 1, 7, 7, fill=GREEN, outline='')
        tk.Label(status_frame, text='감시 중',
                 font=('Segoe UI', 9, 'bold'), bg=SURFACE, fg=GREEN
                 ).pack(side=tk.LEFT)

        # 구분선
        tk.Frame(parent, bg=BORDER, height=1).pack(fill=tk.X)

    # ── 본문 ──
    def _build_body(self, parent: tk.Tk):
        body = tk.PanedWindow(parent, orient=tk.HORIZONTAL,
                              bg=BG, sashwidth=1,
                              sashrelief=tk.FLAT, sashpad=0)
        body.pack(fill=tk.BOTH, expand=True)

        # ── 왼쪽: 카드 목록 ──
        left = tk.Frame(body, bg=BG, width=340)
        body.add(left, minsize=260)

        # 목록 헤더
        lhdr = tk.Frame(left, bg=BG, pady=12, padx=16)
        lhdr.pack(fill=tk.X)
        tk.Label(lhdr, text='크래시 이력',
                 font=('Segoe UI', 9, 'bold'), bg=BG, fg=SUBTLE
                 ).pack(side=tk.LEFT)

        clear_all_lbl = tk.Label(lhdr, text='전체 삭제', font=('Segoe UI', 8),
                                  bg=BG, fg=RED, cursor='hand2')
        clear_all_lbl.pack(side=tk.RIGHT)
        clear_all_lbl.bind('<Button-1>', lambda e: self._clear_all_crash())
        tk.Frame(left, bg=BORDER, height=1).pack(fill=tk.X)

        # 스크롤 가능한 카드 영역
        card_canvas = tk.Canvas(left, bg=BG, highlightthickness=0)
        card_sb = ttk.Scrollbar(left, orient=tk.VERTICAL, command=card_canvas.yview)
        card_canvas.configure(yscrollcommand=card_sb.set)

        self._card_canvas = card_canvas
        self._card_container = tk.Frame(card_canvas, bg=BG)
        _cid = card_canvas.create_window((0, 0), window=self._card_container, anchor='nw')

        def _on_resize(e):
            card_canvas.configure(scrollregion=card_canvas.bbox('all'))
            card_canvas.itemconfig(_cid, width=card_canvas.winfo_width())

        self._card_container.bind('<Configure>', _on_resize)
        card_canvas.bind('<Configure>',
                         lambda e: card_canvas.itemconfig(_cid, width=e.width))

        card_sb.pack(side=tk.RIGHT, fill=tk.Y)
        card_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # ── 오른쪽: 상세 ──
        right = tk.Frame(body, bg=SURFACE)
        body.add(right, minsize=500)

        # 상세 헤더 (선택된 항목 정보)
        self._detail_hdr = tk.Frame(right, bg=SURFACE, pady=18, padx=24)
        self._detail_hdr.pack(fill=tk.X)

        self._lbl_app   = tk.Label(self._detail_hdr, text='항목을 선택하세요',
                                    font=('Segoe UI', 14, 'bold'), bg=SURFACE, fg=FG, anchor=tk.W)
        self._lbl_app.pack(fill=tk.X)

        self._lbl_type  = tk.Label(self._detail_hdr, text='',
                                    font=('Segoe UI', 9), bg=SURFACE, fg=MUTED, anchor=tk.W)
        self._lbl_type.pack(fill=tk.X, pady=(3, 0))

        self._lbl_sum   = tk.Label(self._detail_hdr, text='',
                                    font=('Segoe UI', 9), bg=SURFACE, fg=SUBTLE, anchor=tk.W,
                                    wraplength=700, justify=tk.LEFT)
        self._lbl_sum.pack(fill=tk.X, pady=(6, 0))

        tk.Frame(right, bg=BORDER, height=1).pack(fill=tk.X)

        # ── 아이콘 탭 바 ──
        tab_bar = tk.Frame(right, bg=BG, pady=8, padx=14)
        tab_bar.pack(fill=tk.X)
        tk.Frame(right, bg=BORDER, height=1).pack(fill=tk.X)

        self._detail_tab_btns:   dict = {}
        self._detail_tab_frames: dict = {}

        detail_body = tk.Frame(right, bg=SURFACE)
        detail_body.pack(fill=tk.BOTH, expand=True)

        _tab_analysis_f = tk.Frame(detail_body, bg=SURFACE)
        _tab_raw_f      = tk.Frame(detail_body, bg=SURFACE)
        _tab_report_f   = tk.Frame(detail_body, bg=SURFACE)

        self._detail_tab_frames = {
            'analysis': _tab_analysis_f,
            'raw':      _tab_raw_f,
            'report':   _tab_report_f,
        }

        def _switch_detail(name):
            for n, f in self._detail_tab_frames.items():
                f.pack_forget()
            self._detail_tab_frames[name].pack(fill=tk.BOTH, expand=True)
            for n, b in self._detail_tab_btns.items():
                active = n == name
                b.config(bg=BLUE if active else BG,
                         fg='#FFFFFF' if active else MUTED,
                         font=('Segoe UI', 9, 'bold') if active else ('Segoe UI', 9))

        self._switch_detail = _switch_detail

        for key, icon, label in [('analysis', '⚡', '분석'),
                                   ('raw',      '≡',  '로그'),
                                   ('report',   '◈',  '보고서')]:
            active = key == 'analysis'
            btn = tk.Button(tab_bar, text=f'{icon}  {label}',
                            font=('Segoe UI', 9, 'bold' if active else 'normal'),
                            bg=BLUE if active else BG,
                            fg='#FFFFFF' if active else MUTED,
                            relief=tk.FLAT, bd=0, padx=14, pady=6,
                            cursor='hand2',
                            command=lambda k=key: _switch_detail(k))
            btn.pack(side=tk.LEFT, padx=(0, 6))
            self._detail_tab_btns[key] = btn

        # 분석 탭 콘텐츠
        self._analysis_txt = self._make_text(_tab_analysis_f, font=('Segoe UI', 10))
        self._analysis_txt.tag_configure('head',    font=('Segoe UI', 11, 'bold'), foreground=FG)
        self._analysis_txt.tag_configure('label',   foreground=MUTED)
        self._analysis_txt.tag_configure('value',   foreground=FG)
        self._analysis_txt.tag_configure('exc',     foreground=RED,  font=('Segoe UI', 10, 'bold'))
        self._analysis_txt.tag_configure('stack',   foreground=CYAN, font=('Consolas', 10))
        self._analysis_txt.tag_configure('warn',    foreground=ORANGE)
        self._analysis_txt.tag_configure('sep',     foreground=BORDER)
        self._analysis_txt.tag_configure('section', foreground=BLUE, font=('Segoe UI', 10, 'bold'))

        # 로그 탭 콘텐츠
        self._raw_txt = self._make_text(_tab_raw_f, font=('Consolas', 10), fg=SUBTLE)

        # 보고서 탭 콘텐츠
        self._build_report_tab(_tab_report_f)

        # 기본 탭 표시
        _tab_analysis_f.pack(fill=tk.BOTH, expand=True)

        # ── 카드 생성 (날짜별 그룹) ──
        self._indexed = list(reversed(self._history))
        self._build_date_groups(self._card_container)

        if self._indexed:
            self._select(0)

    def _make_text(self, parent, font=('Consolas', 9), fg=FG) -> scrolledtext.ScrolledText:
        txt = scrolledtext.ScrolledText(
            parent, wrap=tk.WORD,
            bg=SURFACE, fg=fg,
            font=font,
            relief=tk.FLAT, padx=20, pady=16,
            state=tk.DISABLED,
            insertbackground=FG,
            selectbackground=OVERLAY,
            selectforeground=FG,
            borderwidth=0,
        )
        txt.pack(fill=tk.BOTH, expand=True)
        return txt

    def _build_report_tab(self, parent: tk.Frame):
        # 복사 버튼
        btn_frame = tk.Frame(parent, bg=SURFACE, pady=10, padx=16)
        btn_frame.pack(fill=tk.X)
        tk.Frame(parent, bg=BORDER, height=1).pack(fill=tk.X)

        self._copy_btn = tk.Button(
            btn_frame,
            text='📋  개발자 보고서 복사',
            font=('Segoe UI', 9, 'bold'),
            bg=BLUE, fg='#FFFFFF',
            relief=tk.FLAT, padx=16, pady=6,
            cursor='hand2', activebackground=MAUVE, activeforeground='#FFFFFF',
        )
        self._copy_btn.pack(side=tk.LEFT)
        tk.Label(btn_frame, text='클립보드에 복사되면 바로 붙여넣기 가능합니다',
                 font=('Segoe UI', 8), bg=PANEL, fg=MUTED).pack(side=tk.LEFT, padx=10)

        tk.Frame(parent, bg=BORDER, height=1).pack(fill=tk.X)
        self._report_txt = self._make_text(parent, font=('Consolas', 8), fg=SUBTLE)

    def _add_card(self, parent: tk.Frame, ev: CrashEvent, idx: int):
        is_svc   = '서비스' in ev.error_type
        is_error = ev.level == 'Error'
        bar_color  = RED if is_error else (TEAL if is_svc else YELLOW)
        card_tint  = RED_BG if is_error else (BLUE_BG if is_svc else CARD)
        type_color = RED if is_error else (TEAL if is_svc else YELLOW)

        # 외곽 구분선
        tk.Frame(parent, bg=BORDER, height=1).pack(fill=tk.X)

        card = tk.Frame(parent, bg=card_tint, cursor='hand2')
        card.pack(fill=tk.X)

        # 왼쪽 컬러 바
        bar = tk.Frame(card, bg=bar_color, width=4)
        bar.pack(side=tk.LEFT, fill=tk.Y)
        self._card_bars.append(bar)

        inner = tk.Frame(card, bg=card_tint, padx=14, pady=11)
        inner.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 앱 이름 + 시간
        top = tk.Frame(inner, bg=card_tint)
        top.pack(fill=tk.X)
        tk.Label(top, text=ev.app_name,
                 font=('Segoe UI', 9, 'bold'), bg=card_tint, fg=FG, anchor=tk.W
                 ).pack(side=tk.LEFT)
        tk.Label(top, text=ev.timestamp.strftime('%m-%d  %H:%M'),
                 font=('Segoe UI', 8), bg=card_tint, fg=MUTED, anchor=tk.E
                 ).pack(side=tk.RIGHT)

        # 유형 배지
        badge_frame = tk.Frame(inner, bg=card_tint)
        badge_frame.pack(fill=tk.X, pady=(4, 0))
        tk.Label(badge_frame, text=ev.error_type,
                 font=('Segoe UI', 8, 'bold'), bg=card_tint, fg=type_color, anchor=tk.W
                 ).pack(side=tk.LEFT)

        # 요약 (1줄)
        summary_short = ev.summary[:58] + '…' if len(ev.summary) > 58 else ev.summary
        tk.Label(inner, text=summary_short,
                 font=('Segoe UI', 8), bg=card_tint, fg=SUBTLE, anchor=tk.W,
                 wraplength=290, justify=tk.LEFT
                 ).pack(fill=tk.X, pady=(2, 0))

        # 클릭 이벤트
        all_widgets = [card, inner, top, badge_frame] + list(inner.winfo_children()) + list(top.winfo_children())
        for w in all_widgets:
            w.bind('<Button-1>', lambda e, i=idx: self._select(i))
            w.bind('<Enter>',   lambda e, c=card, t=card_tint: c.configure(bg=self._hover_bg(c)))
            w.bind('<Leave>',   lambda e, c=card, t=card_tint: self._restore_bg(c, t))
            w.bind('<MouseWheel>', self._on_mousewheel)

        # 삭제 버튼 (선택 클릭과 겹치지 않도록 위 바인딩 루프 이후 별도 바인딩)
        del_btn = tk.Label(badge_frame, text='✕', font=('Segoe UI', 8, 'bold'),
                            bg=card_tint, fg=MUTED, cursor='hand2')
        del_btn.pack(side=tk.RIGHT)
        del_btn.bind('<Button-1>', lambda e, i=idx: self._delete_crash(i))

        self._card_frames.append(card)
        self._card_tints.append(card_tint)

    def _delete_crash(self, idx: int):
        if not messagebox.askyesno('삭제 확인', '이 크래시 이력을 삭제할까요?'):
            return
        ev = self._indexed.pop(idx)
        if self._on_delete_crash:
            self._on_delete_crash([ev])
        self._rebuild_cards()
        self._update_count()
        if self._indexed:
            self._select(min(idx, len(self._indexed) - 1))
        else:
            self._clear_detail()

    def _clear_all_crash(self):
        if not self._indexed:
            return
        if not messagebox.askyesno(
                '전체 삭제 확인',
                f'크래시 이력 전체 {len(self._indexed)}건을 삭제할까요?\n이 작업은 되돌릴 수 없습니다.'):
            return
        evs = list(self._indexed)
        self._indexed.clear()
        if self._on_delete_crash:
            self._on_delete_crash(evs)
        self._rebuild_cards()
        self._update_count()
        self._clear_detail()

    def _clear_detail(self):
        self._selected = None
        self._lbl_app.config(text='항목을 선택하세요')
        self._lbl_type.config(text='')
        self._lbl_sum.config(text='')
        for txt in (self._analysis_txt, self._raw_txt, self._report_txt):
            txt.config(state=tk.NORMAL)
            txt.delete('1.0', tk.END)
            txt.config(state=tk.DISABLED)

    def _on_mousewheel(self, e):
        delta = -1 * (e.delta // 120)
        w = e.widget
        while w is not None:
            if w is getattr(self, '_card_canvas', None):
                self._card_canvas.yview_scroll(delta, 'units')
                return
            if w is getattr(self, '_install_canvas', None):
                self._install_canvas.yview_scroll(delta, 'units')
                return
            w = getattr(w, 'master', None)

    def _hover_bg(self, frame: tk.Frame) -> str:
        return '#F7FAFC'

    def _restore_bg(self, frame: tk.Frame, color: str):
        frame.configure(bg=color)

    @staticmethod
    def _set_bg(widget, bg, skip):
        if widget in skip:
            return
        try:
            widget.configure(bg=bg)
        except Exception:
            pass
        for child in widget.winfo_children():
            HistoryWindow._set_bg(child, bg, skip)

    def _select(self, idx: int):
        for i, cf in enumerate(self._card_frames):
            tint   = self._card_tints[i] if i < len(self._card_tints) else CARD
            sel_bg = OVERLAY if i == idx else tint
            bar    = self._card_bars[i] if i < len(self._card_bars) else None
            skip   = {bar} if bar else set()
            self._set_bg(cf, sel_bg, skip)

        ev = self._indexed[idx]
        self._selected = ev

        # 헤더 업데이트
        self._lbl_app.config(text=f'{ev.app_name}  ({ev.process})')
        self._lbl_type.config(
            text=f'{ev.error_type}   ·   {ev.timestamp.strftime("%Y-%m-%d %H:%M:%S")}',
            fg=RED if ev.level == 'Error' else YELLOW
        )
        self._lbl_sum.config(text=ev.summary)

        # 탭 내용 채우기
        self._fill_analysis(ev)
        self._fill_raw(ev)
        self._fill_report(ev)

    def _fill_analysis(self, ev: CrashEvent):
        t = self._analysis_txt
        t.config(state=tk.NORMAL)
        t.delete('1.0', tk.END)

        def w(text, tag=None):
            t.insert(tk.END, text, tag)

        # 기본 정보
        w('기본 정보\n', 'section')
        w('─' * 46 + '\n', 'sep')
        w('발생 시각   ', 'label'); w(f'{ev.timestamp.strftime("%Y-%m-%d %H:%M:%S")}\n', 'value')
        w('프로세스    ', 'label'); w(f'{ev.process}\n', 'value')
        w('오류 유형   ', 'label'); w(f'{ev.error_type}\n', 'exc')
        w('\n')

        # 유형별 분석
        if ev.error_type == '.NET 비정상 종료':
            w('예외 정보\n', 'section')
            w('─' * 46 + '\n', 'sep')
            w(f'{ev.summary}\n\n', 'exc')

            stack_lines = [l.strip() for l in ev.detail.splitlines() if l.strip().startswith('at ')]
            if stack_lines:
                w('스택 트레이스\n', 'section')
                w('─' * 46 + '\n', 'sep')
                for line in stack_lines:
                    w(f'  {line}\n', 'stack')
                w('\n')

            w('원인 분석\n', 'section')
            w('─' * 46 + '\n', 'sep')
            if 'AccessViolation' in ev.summary:
                w('네이티브 코드(P/Invoke)에서 잘못된 메모리 접근이 발생했습니다.\n', 'warn')
                w('→ FileWatchCore 네이티브 모듈의 포인터 초기화 또는 경계 처리를 확인하세요.\n', 'value')
            else:
                w('.NET 런타임이 처리되지 않은 예외로 인해 프로세스를 종료했습니다.\n', 'warn')

        elif ev.error_type == '앱 오류 (APPCRASH)':
            w('오류 상세\n', 'section')
            w('─' * 46 + '\n', 'sep')
            for line in ev.detail.splitlines():
                for key in ('Faulting application name', 'Faulting module name',
                            'Exception code', 'Fault offset',
                            'Faulting application path', 'Report Id'):
                    if key + ':' in line:
                        label = key.replace('Faulting ', '').replace(' name', '').strip()
                        val   = line.split(':', 1)[-1].strip().split(',')[0].strip()
                        w(f'{label:<22}', 'label')
                        w(f'{val}\n', 'exc' if 'code' in key.lower() else 'value')
                        break
            w('\n')
            w('원인 분석\n', 'section')
            w('─' * 46 + '\n', 'sep')
            if '메모리 접근 위반' in ev.summary:
                w('0xC0000005: ACCESS_VIOLATION\n', 'exc')
                w('→ 잘못된 주소 역참조 또는 버퍼 오버플로우가 원인일 수 있습니다.\n', 'warn')

            stack_lines = [l.strip() for l in ev.detail.splitlines() if l.strip().startswith('at ')]
            if stack_lines:
                w('\n')
                w('관리 코드 스택 트레이스 (덤프 자동 분석)\n', 'section')
                w('─' * 46 + '\n', 'sep')
                for line in stack_lines:
                    w(f'  {line}\n', 'stack')

        elif ev.error_type == '응답 없음 (Hang)':
            w('원인 분석\n', 'section')
            w('─' * 46 + '\n', 'sep')
            w('AppHangB1: UI 메인 스레드가 메시지 펌프를 멈췄습니다.\n', 'warn')
            w('\n가능한 원인\n', 'label')
            w('  1. UI 스레드에서 서비스 IPC 응답을 동기 대기\n', 'value')
            w('  2. 내부 Deadlock (Lock 경합)\n', 'value')
            w('  3. 서비스 크래시 이후 UI가 연결 끊김 상태에서 대기\n', 'value')

        elif '서비스' in ev.error_type:
            w('원인 분석\n', 'section')
            w('─' * 46 + '\n', 'sep')
            w(f'{ev.process} 서비스가 예기치 않게 중단되었습니다.\n', 'warn')
            w('\n→ 서비스 자동 복구 정책에 따라 재시작되었을 수 있습니다.\n', 'value')
            w('→ System 이벤트 로그에서 복구 동작을 확인하세요.\n', 'value')

        t.config(state=tk.DISABLED)

    def _fill_raw(self, ev: CrashEvent):
        t = self._raw_txt
        t.config(state=tk.NORMAL)
        t.delete('1.0', tk.END)
        t.insert(tk.END, ev.detail)
        t.config(state=tk.DISABLED)

    def _fill_report(self, ev: CrashEvent):
        report = _make_report(ev)
        t = self._report_txt
        t.config(state=tk.NORMAL)
        t.delete('1.0', tk.END)
        t.insert(tk.END, report)
        t.config(state=tk.DISABLED)

        self._copy_btn.config(
            command=lambda: (
                self._root.clipboard_clear(),
                self._root.clipboard_append(report),
                self._copy_btn.config(text='✅  복사됨!'),
                self._root.after(2000, lambda: self._copy_btn.config(text='📋  개발자 보고서 복사')),
            )
        )


# ── 메인 앱 ──────────────────────────────────────────────────────
class QAMonitor:
    def __init__(self):
        self._history: List[CrashEvent] = load_history()
        self._install_history: List[InstallEvent] = load_install_history()
        self._lock = threading.Lock()
        self._icon: Optional[pystray.Icon] = None
        self._watcher = EventLogWatcher(self._on_crash, self._on_install, self._on_poll)
        self._dump_watcher = DumpWatcher(self._on_stack_trace)
        self._open_window: Optional[HistoryWindow] = None
        self._last_checked: Optional[datetime] = None
        self._consec_fail  = 0
        self._alert_showing = False

    def run(self):
        self._watcher.start()
        self._dump_watcher.start()

        menu = pystray.Menu(
            pystray.MenuItem('크래시 이력 보기', self._show_history, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('종료', self._quit),
        )
        self._icon = pystray.Icon(
            name='ezlab-qa-monitor',
            icon=self._make_tray_icon(alert=False),
            title=self._idle_title(),
            menu=menu,
        )

        def on_setup(icon):
            icon.visible = True
            self._notify_start()

        self._icon.run(setup=on_setup)

    def _notify_start(self):
        msg = 'ezFinder · ezCapture · ezCam · ezMemo · ezZip · ezManager 감시 중'
        if HAS_TOAST:
            threading.Thread(
                target=lambda: _toaster.show_toast(
                    'ezLab QA Monitor 시작됨', msg,
                    icon_path=str(LOGO_ICO) if LOGO_ICO.exists() else None,
                    duration=5, threaded=False,
                    callback_on_click=self._show_history,
                ),
                daemon=True,
            ).start()
        else:
            try:
                self._icon.notify(msg, title='ezLab QA Monitor 시작됨')
            except Exception:
                pass

    def _idle_title(self) -> str:
        if self._last_checked:
            return f'ezLab QA Monitor — 감시 중 (마지막 확인 {self._last_checked.strftime("%H:%M:%S")})'
        return 'ezLab QA Monitor — 감시 중'

    def _on_poll(self, ok: bool):
        if ok:
            self._consec_fail = 0
            self._last_checked = datetime.now()
            if self._icon and not self._alert_showing:
                self._icon.title = self._idle_title()
            return

        self._consec_fail += 1
        past_threshold = self._consec_fail - POLL_FAIL_ALERT_THRESHOLD
        if past_threshold == 0 or (past_threshold > 0 and past_threshold % POLL_FAIL_ALERT_REPEAT == 0):
            self._notify_watch_error()

    def _notify_watch_error(self):
        msg = 'Windows 이벤트 로그 읽기가 계속 실패하고 있습니다. 모니터링이 멈췄을 수 있습니다.'
        if HAS_TOAST:
            threading.Thread(
                target=lambda: _toaster.show_toast(
                    '[이지랩 QA] 모니터링 오류', msg,
                    icon_path=str(LOGO_ICO) if LOGO_ICO.exists() else None,
                    duration=8, threaded=False,
                ),
                daemon=True,
            ).start()
        elif self._icon:
            try:
                self._icon.notify(msg, title='[이지랩 QA] 모니터링 오류')
            except Exception:
                pass

    def _on_crash(self, ev: CrashEvent):
        with self._lock:
            self._history.append(ev)
            count = len(self._history)
            win = self._open_window
        append_to_history(ev)
        self._alert_showing = True

        # 창이 열려 있으면 실시간으로 카드 추가
        if win:
            win.push_event(ev)

        if self._icon:
            self._icon.icon  = self._make_tray_icon(alert=True)
            self._icon.title = f'ezLab QA Monitor — {count}건 감지'

        msg = f'{ev.app_name}  |  {ev.error_type}\n{ev.summary[:100]}'
        if HAS_TOAST:
            threading.Thread(
                target=lambda: _toaster.show_toast(
                    '[이지랩 QA] 크래시 감지', msg,
                    icon_path=str(LOGO_ICO) if LOGO_ICO.exists() else None,
                    duration=8, threaded=False,
                    callback_on_click=self._show_history,
                ),
                daemon=True,
            ).start()
        else:
            try:
                self._icon.notify(msg, title='[이지랩 QA] 크래시 감지')
            except Exception:
                pass

    def _on_install(self, ev: InstallEvent):
        with self._lock:
            self._install_history.append(ev)
            win = self._open_window
        append_install_history(ev)
        if win:
            win.push_install(ev)

    def _on_stack_trace(self, proc_name: str, dump_ts: datetime, stack_text: str):
        # 덤프에서 분석해낸 관리 코드 스택 트레이스를, 시간/프로세스명이
        # 가장 가까운 기존 크래시 이력에 덧붙인다. (열려있는 창은 다음에
        # 다시 열 때 갱신된 내용을 보여준다 — 실시간 갱신은 하지 않음)
        with self._lock:
            match = None
            for ev in reversed(self._history):
                if (ev.process.lower() == proc_name.lower()
                        and abs((ev.timestamp - dump_ts).total_seconds()) < 120):
                    match = ev
                    break
            if match is None or '자동 분석된 관리 코드 스택 트레이스' in match.detail:
                return
            match.detail += (
                '\n\n[자동 분석된 관리 코드 스택 트레이스]\n' + '─' * 48 + '\n' + stack_text
            )
            snapshot = list(self._history)
        save_history(snapshot)

    def _show_history(self, icon=None, item=None):
        with self._lock:
            if self._open_window is not None:
                return
            crash_snap   = list(self._history)
            install_snap = list(self._install_history)
            window = HistoryWindow(crash_snap, install_snap,
                                   on_open=self._on_window_open,
                                   on_delete_crash=self._on_delete_crash,
                                   on_delete_install=self._on_delete_install)
            self._open_window = window

        def run_window():
            try:
                window.show()
            finally:
                with self._lock:
                    self._open_window = None

        threading.Thread(target=run_window, daemon=True, name='HistoryWindow').start()

    def _on_window_open(self):
        self._alert_showing = False
        if self._icon:
            self._icon.icon  = self._make_tray_icon(alert=False)
            self._icon.title = self._idle_title()

    def _on_delete_crash(self, evs: List[CrashEvent]):
        ids = {id(e) for e in evs}
        with self._lock:
            self._history = [e for e in self._history if id(e) not in ids]
            snapshot = list(self._history)
        save_history(snapshot)

    def _on_delete_install(self, evs: List[InstallEvent]):
        ids = {id(e) for e in evs}
        with self._lock:
            self._install_history = [e for e in self._install_history if id(e) not in ids]
            snapshot = list(self._install_history)
        save_install_history(snapshot)

    def _quit(self, icon=None, item=None):
        self._watcher.stop()
        self._dump_watcher.stop()
        if self._icon:
            self._icon.stop()

    def _make_tray_icon(self, alert: bool) -> Image.Image:
        if LOGO_PNG.exists():
            try:
                img = Image.open(LOGO_PNG).convert('RGBA').resize((64, 64), Image.LANCZOS)
                if alert:
                    # 오른쪽 하단에 빨간 뱃지
                    overlay = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
                    d = ImageDraw.Draw(overlay)
                    d.ellipse([42, 42, 62, 62], fill='#f7768e')
                    img = Image.alpha_composite(img, overlay)
                return img
            except Exception:
                pass

        # 폴백: 텍스트 아이콘
        sz  = 64
        img = Image.new('RGBA', (sz, sz), (0, 0, 0, 0))
        d   = ImageDraw.Draw(img)
        d.ellipse([2, 2, sz-2, sz-2], fill='#f7768e' if alert else '#7aa2f7')
        try:
            font = ImageFont.truetype('C:/Windows/Fonts/segoeui.ttf', 20)
            d.text((sz//2, sz//2), 'EZ', fill='white', font=font, anchor='mm')
        except Exception:
            d.text((20, 22), 'EZ', fill='white')
        return img


# ── 진입점 ───────────────────────────────────────────────────────
if __name__ == '__main__':
    _register_aumid()

    MUTEX_NAME = 'Global\\ezLabQAMonitor_SingleInstance'
    mutex = win32event.CreateMutex(None, False, MUTEX_NAME)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        win32api.CloseHandle(mutex)
        sys.exit(0)

    try:
        QAMonitor().run()
    finally:
        win32api.CloseHandle(mutex)
