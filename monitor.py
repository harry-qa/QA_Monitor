#!/usr/bin/env python3
"""
ezLab QA Monitor v1.0
이지랩 앱 실시간 크래시 / 로그 감지 도구
"""

import os
import sys
import csv
import gc
import json
import queue
import msvcrt
import zipfile
import threading
import subprocess
from collections import deque
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
from datetime import datetime, timedelta, timezone
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

# Windows 알림 센터 정식 API(WinRT) 기반. 구 win10toast는 유지보수 중단
# (setuptools 81+에서 pkg_resources 제거로 import 자체가 실패)이고 클릭
# 콜백도 포크 전용이라 v1.3.0에서 교체했다.
try:
    from windows_toasts import Toast, ToastDuration, WindowsToaster
    HAS_WINDOWS_TOASTS = True
except Exception:
    HAS_WINDOWS_TOASTS = False


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

# 채널별 마지막으로 처리한 이벤트 레코드 번호. 이걸 영속시켜야
# 모니터가 꺼져 있던 동안 발생한 크래시도 재시작 시 따라잡을 수 있다.
WATCHER_STATE_FILE = DATA_DIR / 'watcher_state.json'

# WER(Windows Error Reporting)이 자동 저장하는 크래시 덤프(.dmp) 위치.
# 서비스 계정이 크래시해도 항상 같은 경로를 쓰도록 %LOCALAPPDATA%가 아니라
# 사용자와 무관한 %ProgramData%를 쓴다 (인스톨러가 이 경로로 LocalDumps를 등록함).
DUMP_DIR          = Path(os.environ.get('ProgramData', r'C:\ProgramData')) / 'ezLab QA Monitor' / 'Dumps'
DUMP_ANALYZER_EXE = BASE_DIR / 'DumpAnalyzer.exe'
MAX_DUMP_FILES    = 20  # 오래된 덤프는 자동 삭제, 최근 N개만 보관

# 이미 분석한 덤프 파일명 목록. 영속시키지 않으면 재시작할 때마다 남아있는
# 덤프 전부를 다시 분석한다 (개당 최대 90초 — 시작 직후 CPU 낭비).
ANALYZED_DUMPS_FILE = DATA_DIR / 'analyzed_dumps.json'

APP_AUMID    = 'EzLab.QAMonitor'
APP_NAME     = 'ezLab QA Monitor'


def _read_version() -> str:
    # VERSION 파일이 단일 출처: installer.iss도 같은 파일을 읽는다.
    try:
        return (BASE_DIR / 'VERSION').read_text(encoding='utf-8').strip()
    except Exception:
        return '?'


APP_VERSION = _read_version()


def _enable_dpi_awareness():
    """고DPI 배율(125%/150%)에서 Windows가 창을 비트맵 확대하지 않게 한다.
    이게 없으면 글자가 뿌옇게 번져서 오래된 프로그램처럼 보인다."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # SYSTEM_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


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
    # 모니터 자신: 실행 중 죽으면 다음 시작 때 백필로 자기 크래시를 잡는다
    'ezlabqamonitor.exe'    : 'ezLab QA Monitor (자체)',
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

# ── WER 부가 보고 상관 ────────────────────────────────────────────
# Windows Error Reporting은 크래시/행 외에도 진단 보고(1001)를 남긴다.
# 예: RADAR_PRE_LEAK(메모리 폭주 감지)는 행/크래시의 원인 힌트가 되므로
# 같은 앱의 근접 이력 상세에 자동으로 첨부한다.
WER_EVENT_ID = 1001

# 1000/1002/1026으로 이미 캡처하는 크래시·행 자체의 WER 보고는 중복 제외
WER_NOISE_EVENTS = {'APPCRASH', 'AppHangB1', 'AppHangXProcB1',
                    'MoAppCrash', 'MoAppHang', 'BEX', 'BEX64', 'CLR20r3'}

WER_EVENT_LABELS = {
    'RADAR_PRE_LEAK_64':    '메모리 사용량 비정상 증가 감지 (Windows 리소스 고갈 감지기)',
    'RADAR_PRE_LEAK_WOW64': '메모리 사용량 비정상 증가 감지 (리소스 고갈 감지기, 32비트 프로세스)',
    'FaultTolerantHeap':    '힙 손상 완화(FTH) 심 적용됨 — 반복적 힙 손상 신호',
}

WER_CORRELATE_SEC   = 90    # 크래시/행 시각과 이 범위 내의 WER 보고를 연관으로 취급
WER_SECTION_TITLE   = '[연관 WER 보고]'

# ── 실시간 응답 없음 감시 ─────────────────────────────────────────
# AppHang(1002)은 사용자가 창을 닫은 뒤에야 기록되고 WER 덤프도 남지 않아
# 사후 분석이 불가능하다. 창이 이 시간 이상 연속 무응답이면 프로세스가
# 살아 있는 동안 미니덤프를 떠 둔다.
HANG_DUMP_AFTER_SEC = 10

# MSI 메이저 업그레이드는 제거(1034)→설치(1033)를 한 트랜잭션에서 연달아 기록한다.
# 이 간격 안의 삭제→설치만 업그레이드로 보고, 그보다 벌어지면 수동 삭제 후 신규 설치로 취급.
UPGRADE_WINDOW_SEC = 60

def _norm_exc_code(code: str) -> str:
    c = (code or '').strip().lower()
    return c[2:] if c.startswith('0x') else c


# 이벤트 인서트의 예외 코드는 '0x' 접두사가 없는 형태(c0000005)로 오므로
# 정규화된 키로 관리한다.
EXCEPTION_CODE_LABELS = {
    'c0000005': '메모리 접근 위반 (ACCESS_VIOLATION)',
    'c0000409': '스택 버퍼 오버런 / Fail-Fast',
    '40000015': 'Fatal App Exit',
    'c0000374': '힙 손상 (Heap Corruption)',
    'e0434352': '.NET 예외',
    '80000003': '브레이크포인트 / 내부 어설션 (라이브러리 패닉)',
    'c00000fd': '스택 오버플로우',
}

# 원인 분석 탭에 보여줄 코드별 설명
EXCEPTION_CODE_ANALYSIS = {
    'c0000005': ('0xC0000005: ACCESS_VIOLATION',
                 '잘못된 주소 역참조 또는 버퍼 오버플로우가 원인일 수 있습니다.\n'
                 '→ 네이티브 모듈(P/Invoke 포함)의 포인터/경계 처리를 확인하세요.'),
    'c0000374': ('0xC0000374: HEAP_CORRUPTION',
                 '힙 메모리 손상 — 이중 해제(double free), 해제 후 사용(use-after-free),\n'
                 '또는 힙 버퍼 오버런 가능성이 높습니다.\n'
                 '→ 충돌 모듈이 ntdll.dll로 나오더라도 실제 원인은 힙을 망가뜨린\n'
                 '  응용 프로그램 코드입니다. Application Verifier/PageHeap으로 추적하세요.'),
    'c0000409': ('0xC0000409: STACK_BUFFER_OVERRUN / FAIL_FAST',
                 '스택 버퍼 오버런이 감지되었거나 프로그램이 fail-fast로 즉시 종료했습니다.\n'
                 '→ 보안 쿠키(/GS) 위반 또는 명시적 __fastfail 호출 여부를 확인하세요.'),
    'e0434352': ('0xE0434352: .NET 예외',
                 '처리되지 않은 .NET 예외로 프로세스가 종료되었습니다.\n'
                 '→ 같은 시각의 .NET Runtime(1026) 이벤트에서 예외 형식과 스택을 확인하세요.'),
    '80000003': ('0x80000003: BREAKPOINT / 내부 어설션',
                 '네이티브 라이브러리가 복구 불가능한 내부 상태를 감지하고 스스로 중단했습니다\n'
                 '(어설션 실패/패닉). 충돌 모듈이 원인 라이브러리를 가리킵니다.'),
    'c00000fd': ('0xC00000FD: STACK_OVERFLOW',
                 '스택 오버플로우 — 무한 재귀 또는 과도한 스택 사용이 원인일 수 있습니다.'),
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
HOVER   = '#F7FAFC'   # 카드 호버
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

RED_BG   = '#FFF5F5'  # 크래시 카드 배경 틴트
BLUE_BG  = '#EBF8FF'  # 서비스 카드 배경 틴트
GREEN_BG = '#F0FFF4'  # '감시 중' 상태 배지 배경

SEARCH_PLACEHOLDER = '검색 (앱 · 요약 · 로그 내용)'


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
    action:    str   # '설치' | '삭제' | '업데이트'


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


# ── 이벤트 로그 감시 ─────────────────────────────────────────────
class EventLogWatcher:
    def __init__(self, on_event: Callable[[CrashEvent, bool], None],
                 on_install: Callable[[InstallEvent], None] = None,
                 on_poll: Callable[[bool], None] = None,
                 on_backfill_done: Callable[[int], None] = None,
                 on_wer: Callable[[datetime, str, str], None] = None):
        self._on_event   = on_event
        self._on_install = on_install
        self._on_poll    = on_poll
        self._on_backfill_done = on_backfill_done
        self._on_wer     = on_wer
        self._stop       = threading.Event()
        # log_name -> last processed RecordNumber. 이전 실행에서 저장한 상태를
        # 이어받아, 모니터가 꺼져 있던 동안의 이벤트를 첫 폴에서 따라잡는다.
        self._last_record: dict = self._load_state()
        self._backfill_pending = set(self._last_record.keys())
        self._backfill_count   = 0
        self._in_backfill      = False

    @staticmethod
    def _load_state() -> dict:
        try:
            with open(WATCHER_STATE_FILE, 'r', encoding='utf-8') as f:
                d = json.load(f)
            return {k: int(v) for k, v in d.items() if k in EVENT_LOGS}
        except Exception:
            return {}

    def _save_state(self):
        try:
            _atomic_write_json(WATCHER_STATE_FILE, self._last_record)
        except Exception:
            pass

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name='LogWatcher').start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(POLL_SECONDS):
            try:
                ok = self._poll()
            except Exception:
                ok = False
            if self._on_poll:
                try:
                    self._on_poll(ok)
                except Exception:
                    pass

    def _poll(self) -> bool:
        """모든 채널을 정상 처리했으면 True. 채널별 예외를 여기서 삼키더라도
        실패 여부는 반환해야 _on_poll의 '감시 멈춤' 경고가 실제로 동작한다."""
        state_before = dict(self._last_record)
        failed = 0
        for log_name in EVENT_LOGS:
            # 이 채널의 첫 폴이면서 이전 실행의 저장 상태가 있으면, 지금
            # 따라잡는 이벤트들은 "모니터 미실행 중 발생분"(백필)이다.
            self._in_backfill = log_name in self._backfill_pending
            try:
                handle = win32evtlog.OpenEventLog(None, log_name)
                # ReadEventLog가 예외를 내도 핸들이 새지 않도록 열자마자
                # try/finally로 닫기를 보장한다.
                try:
                    flags  = (win32evtlog.EVENTLOG_BACKWARDS_READ |
                              win32evtlog.EVENTLOG_SEQUENTIAL_READ)

                    last_rec = self._last_record.get(log_name, None)

                    # 저장 상태도 없는 진짜 첫 폴: 현재 최대 RecordNumber만 기록
                    if last_rec is None:
                        chunk = win32evtlog.ReadEventLog(handle, flags, 0)
                        if chunk:
                            self._last_record[log_name] = max(e.RecordNumber for e in chunk)
                        else:
                            self._last_record[log_name] = 0
                        continue

                    new_max = last_rec
                    newest  = None   # 로그의 현재 최신 RecordNumber (초기화 감지용)
                    to_process = []
                    done = False
                    while not done:
                        chunk = win32evtlog.ReadEventLog(handle, flags, 0)
                        if not chunk:
                            break
                        for ev in chunk:
                            if newest is None:
                                newest = ev.RecordNumber
                            if ev.RecordNumber <= last_rec:
                                done = True
                                break
                            new_max = max(new_max, ev.RecordNumber)
                            eid = ev.EventID & 0xFFFF
                            if (eid in CRASH_EVENT_IDS or eid in INSTALL_EVENT_IDS
                                    or eid == WER_EVENT_ID):
                                to_process.append(ev)

                    # 이벤트 로그가 초기화되어 번호가 뒤로 돌아간 경우: 저장 상태를
                    # 버리고 현재 위치부터 다시 감시 (이 폴의 수집분은 신뢰 불가)
                    if newest is not None and newest < last_rec:
                        new_max = newest
                        to_process = []

                    self._last_record[log_name] = new_max
                finally:
                    # 레코드 포인터는 이미 전진했으므로 닫기 실패가 아래의
                    # 이벤트 발행까지 중단시키면 수집분이 영구 유실된다.
                    try:
                        win32evtlog.CloseEventLog(handle)
                    except Exception:
                        pass

                # 오래된 것부터 순서대로 처리. 레코드 번호는 이미 위에서
                # 전진시켰으므로, 이벤트 1건이 예외를 내도 같은 배치의
                # 나머지가 통째로 유실되지 않게 건별로 격리한다.
                processed = 0
                for ev in reversed(to_process):
                    try:
                        if self._process(log_name, ev):
                            processed += 1
                    except Exception:
                        pass

                # 백필 완료 처리: 두 채널 모두 끝나면 합계를 한 번만 통지
                if log_name in self._backfill_pending:
                    self._backfill_count += processed
                    self._backfill_pending.discard(log_name)
                    if not self._backfill_pending and self._on_backfill_done:
                        try:
                            self._on_backfill_done(self._backfill_count)
                        except Exception:
                            pass

            except Exception:
                failed += 1
        self._in_backfill = False
        if self._last_record != state_before:
            self._save_state()
        return failed == 0

    def _process(self, log_name: str, ev) -> bool:
        """이지랩 관련 이벤트를 실제로 발행했으면 True (백필 집계용)."""
        eid = ev.EventID & 0xFFFF
        ts  = self._to_dt(ev.TimeGenerated)

        # ── WER 부가 보고 (1001: RADAR 메모리 폭주 감지 등) ──
        # inserts: [0]bucket [1]type [2]EventName [3]response [4]cabId [5]P1(대상 exe)...
        if eid == WER_EVENT_ID:
            if ev.SourceName != 'Windows Error Reporting' or not self._on_wer:
                return False
            inserts    = list(ev.StringInserts or [])
            event_name = self._ins(inserts, 2)
            target_exe = self._ins(inserts, 5)
            if (not event_name or not target_exe
                    or event_name in WER_NOISE_EVENTS
                    or not self._is_ezlab(target_exe, target_exe)):
                return False
            try:
                self._on_wer(ts, event_name, target_exe)
            except Exception:
                pass
            return False   # 부가 정보라 크래시/백필 집계에는 넣지 않는다

        # ── 설치/삭제 이벤트 ──
        if eid in INSTALL_EVENT_IDS and ev.SourceName == 'MsiInstaller':
            inserts = list(ev.StringInserts or [])
            install = self._parse_install(ts, eid, inserts)
            if install and self._on_install:
                self._on_install(install)
                return True
            return False

        # ── 크래시 이벤트 ──
        try:
            raw = win32evtlogutil.SafeFormatMessage(ev, ev.SourceName)
            # 메시지 DLL을 못 찾으면 "<The description for Event ID ... could not
            # be found ...>" 폴백이 오는데, 앞에 '<'가 붙을 수 있어 in으로 검사
            if 'The description for Event ID' in raw[:120]:
                raw = ''
        except Exception:
            raw = ''

        inserts = list(ev.StringInserts or [])
        msg     = raw if raw else self._format_inserts(eid, inserts)
        detail  = self._build_detail(log_name, eid, ev.SourceName, ts, msg, inserts, not raw)

        if not self._is_ezlab(detail + ev.SourceName, ev.SourceName):
            return False
        crash = self._parse(ts, eid, msg, detail, inserts)
        if crash:
            self._on_event(crash, self._in_backfill)
            return True
        return False

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
            # 이전에 다른 버전이 설치돼 있었으면 업데이트로 분류하되,
            # 사용자가 삭제한 뒤 시간이 지나 다시 설치한 경우는 신규 설치로 둔다.
            action = '설치'
            history = load_install_history()
            prev = next((e for e in reversed(history)
                         if e.app_name == app_name), None)
            if prev and prev.action == '삭제':
                gap = (ts - prev.timestamp).total_seconds()
                if 0 <= gap <= UPGRADE_WINDOW_SEC:
                    # 업그레이드 트랜잭션 내부의 제거 → 삭제 이전 버전과 비교
                    before = next((e for e in reversed(history)
                                   if e.app_name == app_name
                                   and e.action in ('설치', '업데이트')), None)
                    if before and before.version != version:
                        action = '업데이트'
            elif prev and prev.action in ('설치', '업데이트') and prev.version != version:
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
            1002: ['앱 이름', '버전', '프로세스 ID', '시작 시간', '종료 유형',
                   '앱 경로', 'Report ID', '패키지 이름', '패키지 앱 ID',
                   'Hang 유형'],
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
            if label == '시작 시간':
                val = EventLogWatcher._filetime_str(val)
            lines.append(f'{label:<18}: {val}')
        return '\n'.join(lines) if lines else '(파라미터 없음)'

    @staticmethod
    def _filetime_str(val: str) -> str:
        # 이벤트 로그의 프로세스 시작 시간은 FILETIME(1601-01-01 기준
        # 100ns 단위, UTC) 16진수 문자열로 들어온다.
        try:
            ft = int(str(val).strip(), 16)
            dt = (datetime(1601, 1, 1, tzinfo=timezone.utc)
                  + timedelta(microseconds=ft // 10)).astimezone()
            return f'{val} ({dt.strftime("%Y-%m-%d %H:%M:%S")})'
        except Exception:
            # 값이 손상됐거나 OS 시간 범위를 벗어나면(astimezone은 OSError를
            # 던질 수 있음) 원본 그대로 표시한다
            return val

    @staticmethod
    def _build_detail(log_name, eid, source, ts, msg, inserts, from_inserts) -> str:
        # 파라미터 블록은 Windows 언어/메시지 DLL과 무관하게 항상 만들 수
        # 있으므로 무조건 포함한다 (분석 탭이 이 라벨을 파싱한다).
        lines = [
            f'로그 채널   : {log_name}',
            f'이벤트 ID   : {eid}  ({CRASH_EVENT_IDS.get(eid, "")})',
            f'소스        : {source}',
            f'발생 시각   : {ts.strftime("%Y-%m-%d %H:%M:%S")}',
            '─' * 48,
            '',
            '[파라미터]',
            EventLogWatcher._format_inserts(eid, inserts),
        ]
        if not from_inserts and msg.strip():
            lines += ['', '[이벤트 메시지]', msg.strip()]
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

        exc_label = EXCEPTION_CODE_LABELS.get(_norm_exc_code(code), code)

        return CrashEvent(ts, self._resolve(proc), proc,
                          '앱 오류 (APPCRASH)',
                          f'충돌 모듈: {module}  |  {exc_label}', detail)

    def _hang(self, ts, msg, detail, inserts) -> Optional[CrashEvent]:
        # inserts[0]=앱명, [9]=Hang 유형 (이벤트 1002에는 대기 시간 파라미터가 없음)
        proc      = self._ins(inserts, 0)
        hang_type = self._ins(inserts, 9)

        if not proc:
            # 메시지 텍스트에서 추출
            for line in msg.splitlines():
                if 'program' in line.lower():
                    words = line.split()
                    for i, w in enumerate(words):
                        if w.lower() == 'program' and i + 1 < len(words):
                            proc = words[i + 1]; break
                    break

        summary = 'UI 스레드 응답 없음 — Windows에 의해 강제 종료됨'
        if hang_type:
            summary += f'  |  Hang 유형: {hang_type}'
        return CrashEvent(ts, self._resolve(proc), proc,
                          '응답 없음 (Hang)', summary, detail)

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


# ── 실시간 응답 없음 감시 (종료 전 덤프 선제 캡처) ─────────────────
class HangWatcher:
    """이지랩 앱 최상위 창의 '응답 없음' 상태를 실시간 감시한다.

    AppHang(1002)은 사용자가 창을 닫은 뒤에야 이벤트가 남고 스택도 덤프도
    없다. 여기서는 창이 HANG_DUMP_AFTER_SEC 이상 연속 무응답이면 프로세스가
    살아 있는 동안 전체 메모리 미니덤프를 DUMP_DIR에 저장하고(WER 파일명
    형식이라 DumpWatcher가 그대로 자동 분석) 콜백으로 알린다.
    행이 회복되더라도 QA 관점에서는 보고 대상이므로 이벤트는 유지한다."""

    def __init__(self, on_hang: Callable[[str, int, float, bool], None]):
        self._on_hang    = on_hang
        self._stop       = threading.Event()
        self._hung_since = {}     # pid -> 최초 무응답 감지 시각
        self._dumped     = set()  # 이미 덤프를 뜬 pid (프로세스 종료 시 정리)

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name='HangWatcher').start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(POLL_SECONDS):
            try:
                self._poll()
            except Exception:
                pass

    @staticmethod
    def _ezlab_windows():
        """현재 세션의 가시 최상위 창 중 이지랩 프로세스 소유: [(hwnd, pid, exe)].
        모니터 자신은 제외한다 (자기 창 리빌드 중 오탐 방지)."""
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _cb(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return True
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if not h:
                return True
            try:
                buf  = ctypes.create_unicode_buffer(1024)
                size = ctypes.c_ulong(1024)
                if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    exe = os.path.basename(buf.value)
                    if (exe.lower() in EZLAB_APPS
                            and exe.lower() != 'ezlabqamonitor.exe'):
                        found.append((hwnd, pid.value, exe))
            finally:
                kernel32.CloseHandle(h)
            return True

        user32.EnumWindows(_cb, None)
        return found

    @staticmethod
    def _is_hung(hwnd) -> bool:
        user32 = ctypes.windll.user32
        if user32.IsHungAppWindow(hwnd):
            return True
        # IsHungAppWindow는 사용자 입력 시도가 있어야 갱신되는 경우가 있어
        # WM_NULL 응답 시간으로도 능동 확인한다 (2초 무응답 = 멈춤)
        SMTO_ABORTIFHUNG = 0x0002
        res = ctypes.c_ulong()
        ok = user32.SendMessageTimeoutW(hwnd, 0, 0, 0,
                                        SMTO_ABORTIFHUNG, 2000, ctypes.byref(res))
        return not ok

    def _poll(self):
        now = datetime.now()
        # pid 단위로 집계: 한 프로세스가 창을 여러 개 가질 수 있고(콘솔/보조
        # 창은 응답하는데 메인 창만 멈추는 경우), 창 단위로 판정하면 정상
        # 창이 무응답 상태를 매번 리셋해 감지가 영영 안 된다.
        procs: dict = {}   # pid -> (exe, 하나라도 무응답인가)
        for hwnd, pid, exe in self._ezlab_windows():
            hung = self._is_hung(hwnd)
            _, prev_hung = procs.get(pid, (exe, False))
            procs[pid] = (exe, hung or prev_hung)

        for pid, (exe, hung) in procs.items():
            if not hung:
                self._hung_since.pop(pid, None)
                continue
            first = self._hung_since.setdefault(pid, now)
            hung_secs = (now - first).total_seconds()
            if hung_secs >= HANG_DUMP_AFTER_SEC and pid not in self._dumped:
                self._dumped.add(pid)
                ok = self._write_dump(pid, exe)
                try:
                    self._on_hang(exe, pid, hung_secs, ok)
                except Exception:
                    pass
        # 종료된 프로세스의 상태 정리 (pid 재사용 대비)
        self._hung_since = {p: t for p, t in self._hung_since.items() if p in procs}
        self._dumped &= set(procs)

    @staticmethod
    def _write_dump(pid: int, exe: str) -> bool:
        """전체 메모리 미니덤프 저장. 파일명은 WER LocalDumps 형식
        (<exe>.<pid>.dmp)이라 DumpWatcher/DumpAnalyzer 파이프라인을 그대로 탄다.
        쓰는 동안 DumpWatcher가 미완성 파일을 집지 않도록 임시명 후 교체."""
        try:
            DUMP_DIR.mkdir(parents=True, exist_ok=True)
            path = DUMP_DIR / f'{exe}.{pid}.dmp'
            tmp  = path.with_name(path.name + '.tmp')
            PROCESS_ALL_ACCESS      = 0x1F0FFF
            MiniDumpWithFullMemory  = 0x2
            kernel32 = ctypes.windll.kernel32
            hproc = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
            if not hproc:
                return False
            try:
                with open(tmp, 'wb') as f:
                    ok = ctypes.windll.dbghelp.MiniDumpWriteDump(
                        hproc, pid, msvcrt.get_osfhandle(f.fileno()),
                        MiniDumpWithFullMemory, None, None, None)
            finally:
                kernel32.CloseHandle(hproc)
            if ok:
                os.replace(tmp, path)
            else:
                tmp.unlink(missing_ok=True)
            return bool(ok)
        except Exception:
            return False


# ── 크래시 덤프 감시 (ClrMD 기반 관리 코드 스택 트레이스 자동 분석) ──
class DumpWatcher:
    """WER LocalDumps가 떨어뜨린 .dmp 파일을 감시하다가 DumpAnalyzer.exe로
    관리 코드 스택 트레이스를 뽑아 콜백으로 전달한다. 오래된 덤프는 자동 정리."""

    def __init__(self, on_stack_trace: Callable[[str, datetime, str], None]):
        self._on_stack_trace = on_stack_trace
        self._seen: set = self._load_seen()
        self._stop = threading.Event()

    @staticmethod
    def _load_seen() -> set:
        try:
            with open(ANALYZED_DUMPS_FILE, 'r', encoding='utf-8') as f:
                return set(json.load(f))
        except Exception:
            return set()

    def _save_seen(self):
        try:
            _atomic_write_json(ANALYZED_DUMPS_FILE, sorted(self._seen))
        except Exception:
            pass

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

        changed = False
        for dmp in dumps:
            if dmp.name in self._seen:
                continue
            self._seen.add(dmp.name)
            changed = True
            self._analyze(dmp)

        # 보관 개수 제한: 오래된 것부터 정리
        dumps = sorted(DUMP_DIR.glob('*.dmp'), key=lambda p: p.stat().st_mtime)
        for old in dumps[:-MAX_DUMP_FILES] if len(dumps) > MAX_DUMP_FILES else []:
            try:
                old.unlink()
            except Exception:
                pass

        # 삭제된 덤프의 이름은 상태에서도 정리해 파일이 무한히 크지 않게 한다
        existing = {p.name for p in DUMP_DIR.glob('*.dmp')}
        pruned = self._seen & existing
        if pruned != self._seen:
            self._seen = pruned
            changed = True
        if changed:
            self._save_seen()

    def _analyze(self, dmp_path: Path):
        if not DUMP_ANALYZER_EXE.exists():
            return
        # WER 기본 파일명 형식: <프로세스exe>.<PID>.dmp
        proc_name = dmp_path.stem.rsplit('.', 1)[0]
        try:
            result = subprocess.run(
                [str(DUMP_ANALYZER_EXE), str(dmp_path)],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=90, creationflags=subprocess.CREATE_NO_WINDOW,
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
        self._selected_idx: Optional[int] = None
        self._filter_job = None   # 검색 입력 디바운스용 after() 핸들
        self._card_frames: List[tk.Frame] = []
        self._card_tints:  List[str] = []
        self._card_bars:   List[tk.Frame] = []
        self._root: Optional[tk.Tk] = None
        self._count_var: Optional[tk.StringVar] = None
        self._count_label: Optional[tk.Label] = None
        self._install_frame: Optional[tk.Frame] = None  # 설치 이력 테이블 컨테이너
        # 다른 스레드가 Tk API를 직접 부르면 Tcl이 패닉으로 프로세스를 죽일 수
        # 있다(tcl86t.dll 0x80000003). 외부 스레드는 이 큐에만 넣고, GUI
        # 스레드가 after 루프로 꺼내 반영한다.
        self._push_q: queue.Queue = queue.Queue()
        self._closed = False

    def push_event(self, ev: CrashEvent):
        """스레드 안전: Tk를 건드리지 않고 큐에만 적재."""
        if not self._closed:
            self._push_q.put(('crash', ev))

    def push_install(self, ev: InstallEvent):
        """스레드 안전: Tk를 건드리지 않고 큐에만 적재."""
        if not self._closed:
            self._push_q.put(('install', ev))

    def bring_to_front(self):
        """스레드 안전: 이미 열려 있는 창을 앞으로 가져온다 (트레이 재클릭)."""
        if not self._closed:
            self._push_q.put(('focus', None))

    def _on_new_event(self, ev: CrashEvent):
        self._master.insert(0, ev)
        self._refresh_filter_options()
        self._apply_filter()

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

    @staticmethod
    def _make_collapsible(body: tk.Frame, anchor: tk.Widget, btn,
                          lbl_open: str, lbl_close: str,
                          expanded: bool, **pack_kw):
        """접기/펼치기 토글 콜백 생성. 다시 pack할 때 항상 anchor 바로 아래로
        들어가도록 after=anchor로 고정한다 (pack은 기본적으로 맨 뒤에
        추가되므로, 지정 없이 재pack하면 목록 맨 아래로 밀려남)."""
        state = [expanded]

        def _toggle():
            state[0] = not state[0]
            if state[0]:
                body.pack(after=anchor, **pack_kw)
                btn.config(text=lbl_open)
            else:
                body.pack_forget()
                btn.config(text=lbl_close)

        return _toggle

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

            hdr_btn = tk.Button(
                grp, text=f'▾  {label}',
                font=('Segoe UI', 9, 'bold'),
                bg=BG, fg=SUBTLE,
                relief=tk.FLAT, bd=0, anchor=tk.W,
                padx=16, pady=9, cursor='hand2',
                activebackground=BORDER, activeforeground=FG)
            hdr_btn.config(command=self._make_collapsible(
                cards_frame, grp, hdr_btn,
                f'▾  {label}', f'▸  {label}', expanded=True, fill=tk.X))
            hdr_btn.pack(fill=tk.X)
            tk.Frame(grp, bg=BORDER, height=1).pack(fill=tk.X)

            for idx, ev in items:
                self._add_card(cards_frame, ev, idx)

    def _update_count(self):
        n = len(self._indexed)
        if self._count_var:
            self._count_var.set(f'  {n}건  ')
        if self._count_label:
            try:
                if n > 0:
                    self._count_label.pack(side=tk.RIGHT, padx=(10, 0))
                else:
                    self._count_label.pack_forget()
            except Exception:
                pass

    def show(self):
        # 창 구성 중 예외가 나도 반드시 이 스레드에서 Tk 참조를 정리한다
        try:
            self._show_inner()
        finally:
            self._closed = True
            self._teardown()

    def _show_inner(self):
        root = tk.Tk()
        self._root = root
        root.title('ezLab QA Monitor')
        # DPI 인식 상태에서는 픽셀 크기가 배율만큼 작아 보이므로 창 크기를
        # 실제 DPI에 맞춰 잡는다 (폰트는 pt 단위라 자동으로 커짐)
        try:
            dpi_scale = root.winfo_fpixels('1i') / 96.0
        except Exception:
            dpi_scale = 1.0
        root.geometry(f'{int(1320 * dpi_scale)}x{int(780 * dpi_scale)}')
        root.configure(bg=BG)
        root.minsize(int(900 * dpi_scale), int(560 * dpi_scale))

        # 콤보박스 드롭다운 목록 색상 (전역 옵션으로만 지정 가능)
        root.option_add('*TCombobox*Listbox.background', SURFACE)
        root.option_add('*TCombobox*Listbox.foreground', FG)
        root.option_add('*TCombobox*Listbox.selectBackground', OVERLAY)
        root.option_add('*TCombobox*Listbox.selectForeground', FG)
        root.option_add('*TCombobox*Listbox.font', ('Segoe UI', 9))
        root.option_add('*TCombobox*Listbox.borderWidth', 0)

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

        # 외부 스레드가 큐에 넣은 이벤트를 GUI 스레드에서 꺼내 반영
        def _pump():
            try:
                while True:
                    kind, ev = self._push_q.get_nowait()
                    if kind == 'crash':
                        self._on_new_event(ev)
                    elif kind == 'install':
                        self._on_new_install(ev)
                    elif kind == 'focus':
                        root.deiconify()
                        root.lift()
                        root.focus_force()
                        # 다른 창에 가려져 있으면 lift만으로는 안 올라오는
                        # 경우가 있어 topmost를 잠깐 켰다 끈다
                        root.attributes('-topmost', True)
                        root.after(150, lambda: root.attributes('-topmost', False))
            except queue.Empty:
                pass
            if not self._closed:
                root.after(200, _pump)

        root.after(200, _pump)
        root.mainloop()

    def _teardown(self):
        # 반드시 창을 만든 바로 그 스레드에서 실행할 것.
        # (1) 남은 Tk 객체가 다른 스레드의 GC에서 해제되거나
        # (2) tkinter 전역 _default_root가 이 창을 계속 붙들고 있다가
        #     다음 창을 여는 다른 스레드에서 해제되면
        # Tcl_AsyncDelete 패닉(0x80000003)으로 프로세스 전체가 즉사한다.
        if self._root is not None:
            try:
                self._root.destroy()
            except Exception:
                pass
        try:
            if getattr(tk, '_default_root', None) is not None:
                tk._default_root = None
        except Exception:
            pass
        keep = {'_history', '_install_history', '_master', '_selected',
                '_on_open', '_on_delete_crash', '_on_delete_install',
                '_push_q', '_closed'}
        for k in list(self.__dict__.keys()):
            if k not in keep:
                self.__dict__[k] = None
        gc.collect()

    # ── 상단 탭 스위처 (밑줄 인디케이터 스타일) ──
    def _build_tab_switcher(self, parent: tk.Tk):
        bar = tk.Frame(parent, bg=SURFACE)
        bar.pack(fill=tk.X)

        self._tab_btns = {}
        self._active_tab = 'crash'

        def switch(name):
            self._active_tab = name
            if name == 'crash':
                self._crash_frame.pack(fill=tk.BOTH, expand=True)
                self._install_view.pack_forget()
            else:
                self._install_view.pack(fill=tk.BOTH, expand=True)
                self._crash_frame.pack_forget()
            for n, (lbl, ind) in self._tab_btns.items():
                active = n == name
                lbl.config(fg=BLUE if active else MUTED,
                           font=('Segoe UI', 10, 'bold' if active else 'normal'))
                ind.config(bg=BLUE if active else SURFACE)

        for key, label in [('crash', '크래시 이력'), ('install', '설치 / 삭제 이력')]:
            active = key == 'crash'
            cell = tk.Frame(bar, bg=SURFACE, cursor='hand2')
            cell.pack(side=tk.LEFT)
            lbl = tk.Label(cell, text=label,
                           font=('Segoe UI', 10, 'bold' if active else 'normal'),
                           bg=SURFACE, fg=BLUE if active else MUTED,
                           padx=18, pady=9)
            lbl.pack()
            ind = tk.Frame(cell, bg=BLUE if active else SURFACE, height=2)
            ind.pack(fill=tk.X)

            def _hover(on, k=key, l=lbl):
                if k != self._active_tab:
                    l.config(fg=SUBTLE if on else MUTED)

            for w in (cell, lbl):
                w.bind('<Button-1>', lambda e, k=key: switch(k))
                w.bind('<Enter>', lambda e, h=_hover: h(True))
                w.bind('<Leave>', lambda e, h=_hover: h(False))
            self._tab_btns[key] = (lbl, ind)

        tk.Frame(parent, bg=BORDER, height=1).pack(fill=tk.X)

    # ── 설치 이력 뷰 ──
    def _build_install_view(self, parent: tk.Frame):
        # 헤더
        hdr = tk.Frame(parent, bg=SURFACE, pady=10, padx=16)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text='앱별 설치 / 삭제 이력',
                 font=('Segoe UI', 10, 'bold'), bg=SURFACE, fg=FG).pack(side=tk.LEFT)
        tk.Label(hdr, text='MobSoft 제품만 표시됩니다',
                 font=('Segoe UI', 9), bg=SURFACE, fg=MUTED).pack(side=tk.LEFT, padx=12)
        clear_all_lbl = tk.Label(hdr, text='전체 삭제', font=('Segoe UI', 9),
                                  bg=SURFACE, fg=RED, cursor='hand2')
        clear_all_lbl.pack(side=tk.RIGHT)
        clear_all_lbl.bind('<Button-1>', lambda e: self._clear_all_install())

        csv_lbl = tk.Label(hdr, text='CSV 내보내기', font=('Segoe UI', 9),
                            bg=SURFACE, fg=BLUE, cursor='hand2')
        csv_lbl.pack(side=tk.RIGHT, padx=(0, 12))
        csv_lbl.bind('<Button-1>', lambda e: self._export_install_csv())
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

            # 기본은 접힌 상태 — 헤더 클릭 시 _make_collapsible이 after=card로 pack
            rows_frame = tk.Frame(self._install_frame, bg=BG)

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
                     font=('Segoe UI', 9), bg=PANEL, fg=MUTED
                     ).pack(side=tk.LEFT, padx=8)

            latest_color = GREEN if latest.action == '설치' else YELLOW if latest.action == '업데이트' else RED
            tk.Label(hdr, text=f'최근: {latest.action} · {latest.version}',
                     font=('Segoe UI', 9), bg=PANEL, fg=latest_color
                     ).pack(side=tk.RIGHT, padx=14)

            _toggle = self._make_collapsible(rows_frame, card, toggle_btn,
                                             '▾', '▸', expanded=False,
                                             fill=tk.X, padx=16)
            for w in (hdr, toggle_btn):
                w.bind('<Button-1>', lambda e, fn=_toggle: fn())
            for child in hdr.winfo_children():
                child.bind('<Button-1>', lambda e, fn=_toggle: fn())

            # 이벤트 행 (기본은 접혀있음, 헤더 클릭 시 펼침)
            for ev in events:
                row = tk.Frame(rows_frame, bg=CARD)
                row.pack(fill=tk.X)
                tk.Frame(rows_frame, bg=BORDER, height=1).pack(fill=tk.X)

                action_color = GREEN if ev.action == '설치' else YELLOW if ev.action == '업데이트' else RED
                # 액션 뱃지
                tk.Label(row,
                         text=f'  {ev.action}  ',
                         font=('Segoe UI', 9, 'bold'),
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

    def _export_install_csv(self):
        if not self._install_history:
            messagebox.showinfo('내보내기', '내보낼 설치/삭제 이력이 없습니다.')
            return
        path = filedialog.asksaveasfilename(
            parent=self._root, defaultextension='.csv',
            filetypes=[('CSV 파일', '*.csv')],
            initialfile=f'install_history_{datetime.now().strftime("%Y%m%d")}.csv')
        if not path:
            return
        try:
            with open(path, 'w', newline='', encoding='utf-8-sig') as f:
                w = csv.writer(f)
                w.writerow(['시각', '앱', '동작', '버전'])
                for e in sorted(self._install_history,
                                key=lambda x: x.timestamp, reverse=True):
                    w.writerow([e.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
                                e.app_name, e.action, e.version])
        except Exception as ex:
            messagebox.showerror('내보내기 실패', f'파일 저장 중 오류가 발생했습니다.\n{ex}')
            return
        messagebox.showinfo('내보내기 완료', f'{len(self._install_history)}건을 저장했습니다.\n{path}')

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
        # 콤보박스: clam 기본(회색 음영, 구식 느낌) 대신 플랫 화이트
        s.configure('TCombobox',
                    fieldbackground=SURFACE, background=SURFACE,
                    foreground=FG, arrowcolor=SUBTLE,
                    bordercolor=BORDER, lightcolor=SURFACE, darkcolor=SURFACE,
                    padding=(8, 4))
        s.map('TCombobox',
              fieldbackground=[('readonly', SURFACE)],
              foreground=[('readonly', FG)],
              selectbackground=[('readonly', SURFACE)],
              selectforeground=[('readonly', FG)],
              bordercolor=[('focus', BLUE)],
              arrowcolor=[('active', BLUE)])

    # ── 헤더 ──
    def _build_header(self, parent: tk.Tk):
        hdr = tk.Frame(parent, bg=SURFACE, height=64)
        hdr.pack(fill=tk.X)
        hdr.pack_propagate(False)

        # 로고
        if LOGO_PNG.exists():
            try:
                logo = Image.open(LOGO_PNG).convert('RGBA').resize((32, 32), Image.LANCZOS)
                self._logo_photo = ImageTk.PhotoImage(logo)
                tk.Label(hdr, image=self._logo_photo, bg=SURFACE).pack(
                    side=tk.LEFT, padx=(20, 10), pady=16)
            except Exception:
                pass

        title_frame = tk.Frame(hdr, bg=SURFACE)
        title_frame.pack(side=tk.LEFT, pady=16)
        title_row = tk.Frame(title_frame, bg=SURFACE)
        title_row.pack(anchor=tk.W)
        tk.Label(title_row, text='ezLab QA Monitor',
                 font=('Segoe UI', 14, 'bold'), bg=SURFACE, fg=FG
                 ).pack(side=tk.LEFT)
        tk.Label(title_row, text=f'v{APP_VERSION}',
                 font=('Segoe UI', 9), bg=SURFACE, fg=MUTED
                 ).pack(side=tk.LEFT, padx=(8, 0), pady=(6, 0))

        # 오른쪽 상태
        right = tk.Frame(hdr, bg=SURFACE)
        right.pack(side=tk.RIGHT, padx=20, pady=16)

        n = len(self._history)
        self._count_var = tk.StringVar(value=f'  {n}건  ')
        self._count_label = tk.Label(right, textvariable=self._count_var,
                                     font=('Segoe UI', 9, 'bold'),
                                     bg=RED, fg='#FFFFFF', padx=6, pady=3)
        if n > 0:
            self._count_label.pack(side=tk.RIGHT, padx=(10, 0))

        # '감시 중' 상태 배지 (연녹색 칩)
        status_frame = tk.Frame(right, bg=GREEN_BG, padx=10, pady=4)
        status_frame.pack(side=tk.RIGHT)
        dot_canvas = tk.Canvas(status_frame, width=8, height=8, bg=GREEN_BG, highlightthickness=0)
        dot_canvas.pack(side=tk.LEFT, padx=(0, 5))
        dot_canvas.create_oval(1, 1, 7, 7, fill=GREEN, outline='')
        tk.Label(status_frame, text='감시 중',
                 font=('Segoe UI', 9, 'bold'), bg=GREEN_BG, fg=GREEN
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

        clear_all_lbl = tk.Label(lhdr, text='전체 삭제', font=('Segoe UI', 9),
                                  bg=BG, fg=RED, cursor='hand2')
        clear_all_lbl.pack(side=tk.RIGHT)
        clear_all_lbl.bind('<Button-1>', lambda e: self._clear_all_crash())

        csv_lbl = tk.Label(lhdr, text='CSV 내보내기', font=('Segoe UI', 9),
                            bg=BG, fg=BLUE, cursor='hand2')
        csv_lbl.pack(side=tk.RIGHT, padx=(0, 12))
        csv_lbl.bind('<Button-1>', lambda e: self._export_crash_csv())

        # 필터 (앱 / 오류 유형 / 기간)
        fbar = tk.Frame(left, bg=BG)
        fbar.pack(fill=tk.X, padx=16, pady=(0, 6))
        self._filter_app  = ttk.Combobox(fbar, state='readonly', width=13,
                                          font=('Segoe UI', 9))
        self._filter_type = ttk.Combobox(fbar, state='readonly', width=16,
                                          font=('Segoe UI', 9))
        self._filter_app.pack(side=tk.LEFT)
        self._filter_type.pack(side=tk.LEFT, padx=(6, 0))
        self._filter_app.bind('<<ComboboxSelected>>', self._apply_filter)
        self._filter_type.bind('<<ComboboxSelected>>', self._apply_filter)

        # 검색 + 기간
        fbar2 = tk.Frame(left, bg=BG)
        fbar2.pack(fill=tk.X, padx=16, pady=(0, 10))

        self._filter_period = ttk.Combobox(
            fbar2, state='readonly', width=9, font=('Segoe UI', 9),
            values=['전체 기간', '오늘', '최근 7일', '최근 30일'])
        self._filter_period.set('전체 기간')
        self._filter_period.pack(side=tk.RIGHT, padx=(6, 0))
        self._filter_period.bind('<<ComboboxSelected>>', self._apply_filter)

        self._search_var   = tk.StringVar()
        self._search_is_ph = False
        ent = tk.Entry(fbar2, textvariable=self._search_var,
                       font=('Segoe UI', 9), bg=SURFACE, fg=FG,
                       relief=tk.FLAT, insertbackground=FG,
                       highlightthickness=1, highlightbackground=BORDER,
                       highlightcolor=BLUE)
        ent.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=2)
        self._search_entry = ent

        def _ph_show():
            self._search_is_ph = True
            self._search_var.set(SEARCH_PLACEHOLDER)
            ent.config(fg=MUTED)

        def _ph_focus_in(_e):
            if self._search_is_ph:
                self._search_is_ph = False
                self._search_var.set('')
                ent.config(fg=FG)

        def _ph_focus_out(_e):
            if not self._search_var.get().strip():
                _ph_show()

        ent.bind('<FocusIn>',  _ph_focus_in)
        ent.bind('<FocusOut>', _ph_focus_out)
        ent.bind('<KeyRelease>', self._schedule_filter)
        _ph_show()

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
                                    font=('Segoe UI', 15, 'bold'), bg=SURFACE, fg=FG, anchor=tk.W)
        self._lbl_app.pack(fill=tk.X)

        self._lbl_type  = tk.Label(self._detail_hdr, text='',
                                    font=('Segoe UI', 10), bg=SURFACE, fg=MUTED, anchor=tk.W)
        self._lbl_type.pack(fill=tk.X, pady=(3, 0))

        self._lbl_sum   = tk.Label(self._detail_hdr, text='',
                                    font=('Segoe UI', 10), bg=SURFACE, fg=SUBTLE, anchor=tk.W,
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
                         font=('Segoe UI', 10, 'bold') if active else ('Segoe UI', 10))

        self._switch_detail = _switch_detail

        for key, icon, label in [('analysis', '⚡', '분석'),
                                   ('raw',      '≡',  '로그'),
                                   ('report',   '◈',  '보고서')]:
            active = key == 'analysis'
            btn = tk.Button(tab_bar, text=f'{icon}  {label}',
                            font=('Segoe UI', 10, 'bold' if active else 'normal'),
                            bg=BLUE if active else BG,
                            fg='#FFFFFF' if active else MUTED,
                            relief=tk.FLAT, bd=0, padx=16, pady=7,
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
        # _master = 전체 이력(최신순), _indexed = 필터 적용된 표시 뷰
        self._master = list(reversed(self._history))
        self._refresh_filter_options()
        self._apply_filter()

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
            font=('Segoe UI', 10, 'bold'),
            bg=BLUE, fg='#FFFFFF',
            relief=tk.FLAT, padx=18, pady=7,
            cursor='hand2', activebackground=MAUVE, activeforeground='#FFFFFF',
        )
        self._copy_btn.pack(side=tk.LEFT)

        self._zip_btn = tk.Button(
            btn_frame,
            text='📦  보고서+덤프 ZIP 저장',
            font=('Segoe UI', 10, 'bold'),
            bg=TEAL, fg='#FFFFFF',
            relief=tk.FLAT, padx=18, pady=7,
            cursor='hand2', activebackground=GREEN, activeforeground='#FFFFFF',
        )
        self._zip_btn.pack(side=tk.LEFT, padx=(10, 0))
        tk.Label(btn_frame, text='복사는 클립보드로, ZIP은 연관 덤프(.dmp)까지 함께 저장합니다',
                 font=('Segoe UI', 9), bg=SURFACE, fg=MUTED).pack(side=tk.LEFT, padx=10)

        tk.Frame(parent, bg=BORDER, height=1).pack(fill=tk.X)
        self._report_txt = self._make_text(parent, font=('Consolas', 9), fg=SUBTLE)

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

        inner = tk.Frame(card, bg=card_tint, padx=16, pady=12)
        inner.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 앱 이름 + 시간
        top = tk.Frame(inner, bg=card_tint)
        top.pack(fill=tk.X)
        tk.Label(top, text=ev.app_name,
                 font=('Segoe UI', 10, 'bold'), bg=card_tint, fg=FG, anchor=tk.W
                 ).pack(side=tk.LEFT)
        tk.Label(top, text=ev.timestamp.strftime('%m-%d  %H:%M'),
                 font=('Segoe UI', 9), bg=card_tint, fg=MUTED, anchor=tk.E
                 ).pack(side=tk.RIGHT)

        # 유형 배지
        badge_frame = tk.Frame(inner, bg=card_tint)
        badge_frame.pack(fill=tk.X, pady=(4, 0))
        tk.Label(badge_frame, text=ev.error_type,
                 font=('Segoe UI', 9, 'bold'), bg=card_tint, fg=type_color, anchor=tk.W
                 ).pack(side=tk.LEFT)

        # 요약 (1줄)
        summary_short = ev.summary[:58] + '…' if len(ev.summary) > 58 else ev.summary
        tk.Label(inner, text=summary_short,
                 font=('Segoe UI', 9), bg=card_tint, fg=SUBTLE, anchor=tk.W,
                 wraplength=300, justify=tk.LEFT
                 ).pack(fill=tk.X, pady=(3, 0))

        # 클릭/호버 이벤트 — 선택된 카드는 호버로 하이라이트를 덮지 않는다
        def _hover(on, i=idx, c=card, b=bar, t=card_tint):
            if i == self._selected_idx:
                return
            self._set_bg(c, HOVER if on else t, {b})

        all_widgets = [card, inner, top, badge_frame] + list(inner.winfo_children()) + list(top.winfo_children())
        for w in all_widgets:
            w.bind('<Button-1>', lambda e, i=idx: self._select(i))
            w.bind('<Enter>',   lambda e: _hover(True))
            w.bind('<Leave>',   lambda e: _hover(False))
            w.bind('<MouseWheel>', self._on_mousewheel)

        # 삭제 버튼 (선택 클릭과 겹치지 않도록 위 바인딩 루프 이후 별도 바인딩)
        del_btn = tk.Label(badge_frame, text='✕', font=('Segoe UI', 8, 'bold'),
                            bg=card_tint, fg=MUTED, cursor='hand2')
        del_btn.pack(side=tk.RIGHT)
        del_btn.bind('<Button-1>', lambda e, i=idx: self._delete_crash(i))

        self._card_frames.append(card)
        self._card_tints.append(card_tint)

    def _refresh_filter_options(self):
        apps  = sorted({e.app_name for e in self._master})
        types = sorted({e.error_type for e in self._master})
        cur_a = self._filter_app.get()
        cur_t = self._filter_type.get()
        self._filter_app['values']  = ['전체 앱'] + apps
        self._filter_type['values'] = ['전체 유형'] + types
        self._filter_app.set(cur_a if cur_a in apps else '전체 앱')
        self._filter_type.set(cur_t if cur_t in types else '전체 유형')

    def _schedule_filter(self, event=None):
        # 키 입력마다 카드 전체를 다시 그리면 목록이 클 때 버벅이므로 디바운스
        if self._root is None:
            return
        if self._filter_job is not None:
            try:
                self._root.after_cancel(self._filter_job)
            except Exception:
                pass
        self._filter_job = self._root.after(250, self._apply_filter)

    def _apply_filter(self, event=None):
        self._filter_job = None
        fa = self._filter_app.get()
        ft = self._filter_type.get()
        fp = self._filter_period.get()
        evs = self._master
        if fa and fa != '전체 앱':
            evs = [e for e in evs if e.app_name == fa]
        if ft and ft != '전체 유형':
            evs = [e for e in evs if e.error_type == ft]

        if fp and fp != '전체 기간':
            now = datetime.now()
            cutoff = {
                '오늘':     now.replace(hour=0, minute=0, second=0, microsecond=0),
                '최근 7일':  now - timedelta(days=7),
                '최근 30일': now - timedelta(days=30),
            }.get(fp)
            if cutoff:
                evs = [e for e in evs if e.timestamp >= cutoff]

        q = '' if self._search_is_ph else self._search_var.get().strip().lower()
        if q:
            evs = [e for e in evs
                   if q in e.app_name.lower() or q in e.process.lower()
                   or q in e.error_type.lower() or q in e.summary.lower()
                   or q in e.detail.lower()]

        self._indexed = list(evs)
        self._rebuild_cards()
        self._update_count()
        if not self._indexed:
            self._clear_detail()
            return
        # 보고 있던 항목이 여전히 목록에 있으면 선택을 유지한다
        # (실시간 이벤트가 들어올 때마다 맨 위로 튀지 않도록)
        keep = next((i for i, e in enumerate(self._indexed)
                     if e is self._selected), 0)
        self._select(keep)

    def _remove_from_master(self, evs: List[CrashEvent]):
        ids = {id(e) for e in evs}
        self._master = [e for e in self._master if id(e) not in ids]

    def _delete_crash(self, idx: int):
        if not messagebox.askyesno('삭제 확인', '이 크래시 이력을 삭제할까요?'):
            return
        ev = self._indexed[idx]
        self._remove_from_master([ev])
        if self._on_delete_crash:
            self._on_delete_crash([ev])
        self._refresh_filter_options()
        self._apply_filter()

    def _clear_all_crash(self):
        if not self._indexed:
            return
        filtered = len(self._indexed) != len(self._master)
        label = '표시된' if filtered else '전체'
        if not messagebox.askyesno(
                '전체 삭제 확인',
                f'{label} 크래시 이력 {len(self._indexed)}건을 삭제할까요?\n이 작업은 되돌릴 수 없습니다.'):
            return
        evs = list(self._indexed)
        self._remove_from_master(evs)
        if self._on_delete_crash:
            self._on_delete_crash(evs)
        self._refresh_filter_options()
        self._apply_filter()

    def _export_crash_csv(self):
        if not self._indexed:
            messagebox.showinfo('내보내기', '내보낼 크래시 이력이 없습니다.')
            return
        path = filedialog.asksaveasfilename(
            parent=self._root, defaultextension='.csv',
            filetypes=[('CSV 파일', '*.csv')],
            initialfile=f'crash_history_{datetime.now().strftime("%Y%m%d")}.csv')
        if not path:
            return
        try:
            # utf-8-sig: 한글이 Excel에서 깨지지 않도록 BOM 포함
            with open(path, 'w', newline='', encoding='utf-8-sig') as f:
                w = csv.writer(f)
                w.writerow(['발생 시각', '앱', '프로세스', '오류 유형', '요약'])
                for e in self._indexed:
                    w.writerow([e.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
                                e.app_name, e.process, e.error_type, e.summary])
        except Exception as ex:
            messagebox.showerror('내보내기 실패', f'파일 저장 중 오류가 발생했습니다.\n{ex}')
            return
        messagebox.showinfo('내보내기 완료', f'{len(self._indexed)}건을 저장했습니다.\n{path}')

    def _clear_detail(self):
        self._selected = None
        self._selected_idx = None
        self._lbl_app.config(text='항목을 선택하세요')
        self._lbl_type.config(text='')
        self._lbl_sum.config(text='')
        for txt in (self._analysis_txt, self._raw_txt, self._report_txt):
            txt.config(state=tk.NORMAL)
            txt.delete('1.0', tk.END)
            txt.config(state=tk.DISABLED)
        # 삭제된 항목의 리포트가 복사/저장되지 않도록 버튼도 초기화
        self._copy_btn.config(command=lambda: None)
        self._zip_btn.config(command=lambda: None)

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
        self._selected_idx = idx
        for i, cf in enumerate(self._card_frames):
            tint   = self._card_tints[i] if i < len(self._card_tints) else CARD
            sel_bg = OVERLAY if i == idx else tint
            bar    = self._card_bars[i] if i < len(self._card_bars) else None
            skip   = {bar} if bar else set()
            self._set_bg(cf, sel_bg, skip)

        ev = self._indexed[idx]
        if ev is self._selected:
            # 같은 항목 재선택(목록 갱신 등): 탭을 다시 채우면 읽던 스크롤
            # 위치가 초기화되므로 하이라이트만 갱신하고 끝낸다
            return
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
            # 1순위: 우리가 detail에 항상 넣는 [파라미터] 블록 (로캘 무관)
            # 2순위: 영어 포맷 이벤트 메시지 (예전 이력 호환)
            param_keys = ('앱 이름', '버전', '모듈 이름', '모듈 버전',
                          '예외 코드', '오프셋', '앱 경로', '모듈 경로', 'Report ID')
            eng_keys   = ('Faulting application name', 'Faulting module name',
                          'Exception code', 'Fault offset',
                          'Faulting application path', 'Report Id')
            exc_code = ''
            shown = set()
            for line in ev.detail.splitlines():
                s = line.strip()
                for key in param_keys:
                    if s.startswith(key) and ':' in s and key not in shown:
                        val = s.split(':', 1)[1].strip()
                        if not val:
                            break
                        shown.add(key)
                        w(f'{key:<14}', 'label')
                        w(f'{val}\n', 'exc' if key == '예외 코드' else 'value')
                        if key == '예외 코드':
                            exc_code = val
                        break
                else:
                    for key in eng_keys:
                        if key + ':' in line and key not in shown:
                            shown.add(key)
                            label = key.replace('Faulting ', '').replace(' name', '').strip()
                            val   = line.split(':', 1)[-1].strip().split(',')[0].strip()
                            w(f'{label:<22}', 'label')
                            w(f'{val}\n', 'exc' if 'code' in key.lower() else 'value')
                            if 'code' in key.lower():
                                exc_code = val
                            break
            w('\n')
            w('원인 분석\n', 'section')
            w('─' * 46 + '\n', 'sep')
            info = EXCEPTION_CODE_ANALYSIS.get(_norm_exc_code(exc_code))
            if info:
                title, desc = info
                w(title + '\n', 'exc')
                for dl in desc.splitlines():
                    w(dl + '\n', 'warn' if dl.startswith('→') or not dl.startswith(' ') else 'value')
            elif exc_code:
                w(f'예외 코드 {exc_code}\n', 'exc')
                w('알려진 패턴이 아닙니다. 충돌 모듈과 오프셋을 개발자에게 전달하세요.\n', 'value')
            else:
                w('세부 예외 코드를 확인할 수 없습니다. 로그 탭의 원문을 확인하세요.\n', 'value')

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
            w('UI 메인 스레드가 메시지 펌프를 멈춰 Windows가 프로세스를 종료했습니다.\n', 'warn')
            hang_type = ''
            for line in ev.detail.splitlines():
                s = line.strip()
                if s.startswith('Hang 유형') and ':' in s:
                    hang_type = s.split(':', 1)[1].strip()
                    break
            if hang_type:
                w('\n')
                w('Hang 유형    ', 'label'); w(f'{hang_type}\n', 'value')
                if 'idle' in hang_type.lower():
                    w('             (창은 살아있으나 입력을 처리하지 못하는 상태에서 종료됨)\n', 'label')
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
        self._zip_btn.config(command=lambda: self._export_report_zip(ev))

    @staticmethod
    def _matching_dumps(ev: CrashEvent, window_sec: int = 300) -> list:
        """이벤트와 프로세스명·시각이 맞는 덤프 파일 목록.
        파일명은 WER LocalDumps 형식 <exe>.<pid>.dmp (HangWatcher도 동일)."""
        out = []
        try:
            for dmp in DUMP_DIR.glob('*.dmp'):
                base = dmp.stem.rsplit('.', 1)[0]
                if base.lower() != ev.process.lower():
                    continue
                mtime = datetime.fromtimestamp(dmp.stat().st_mtime)
                if abs((ev.timestamp - mtime).total_seconds()) <= window_sec:
                    out.append(dmp)
        except Exception:
            pass
        return out

    @staticmethod
    def _analyze_dump_text(dmp: Path) -> Optional[str]:
        """DumpAnalyzer --full로 사람이 읽는 전체 분석 리포트를 뽑는다.
        분석기가 없거나 실패하면 None (원본 덤프만 ZIP에 담기고 진행)."""
        if not DUMP_ANALYZER_EXE.exists():
            return None
        try:
            result = subprocess.run(
                [str(DUMP_ANALYZER_EXE), str(dmp), '--full'],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=180, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            text = (result.stdout or '').strip()
        except Exception:
            return None
        if not text or text.startswith(('NO_CLR_RUNTIME_FOUND',
                                        'NO_MANAGED_THREAD_FOUND', 'ANALYZER_ERROR')):
            return None
        return text

    def _export_report_zip(self, ev: CrashEvent):
        """개발자 전달용 ZIP: 보고서 텍스트 + 연관 덤프(.dmp) + 각 덤프의 사람이
        읽는 분석 리포트(.txt)를 한 파일로. 덤프 분석은 수십 초 걸릴 수 있어
        워커 스레드에서 수행하고 버튼에 진행 상태를 표시한다."""
        proc_base = ev.process.rsplit('.', 1)[0] or 'unknown'
        path = filedialog.asksaveasfilename(
            defaultextension='.zip',
            filetypes=[('ZIP 파일', '*.zip')],
            initialfile=(f'ezlab_report_{proc_base}_'
                         f'{ev.timestamp.strftime("%Y%m%d_%H%M%S")}.zip'))
        if not path:
            return
        dumps = self._matching_dumps(ev)

        # 진행 표시 + 중복 클릭 방지
        self._zip_btn.config(state=tk.DISABLED,
                             text='📦  분석 중… (수십 초 소요)')

        def _finish(err, saved_dumps, analyzed):
            self._zip_btn.config(state=tk.NORMAL, text='📦  보고서+덤프 ZIP 저장')
            if err:
                messagebox.showerror('내보내기 실패', f'ZIP 저장 중 오류가 발생했습니다.\n{err}')
            elif saved_dumps:
                names = '\n'.join(f'  · {n}' for n in saved_dumps)
                extra = (f'\n분석 리포트 {analyzed}개 포함(.dmp를 열지 않아도 스택 확인 가능)'
                         if analyzed else '')
                messagebox.showinfo('내보내기 완료',
                                    f'보고서와 덤프 {len(saved_dumps)}개를 저장했습니다.'
                                    f'{extra}\n{names}\n\n{path}')
            else:
                messagebox.showinfo('내보내기 완료',
                                    f'보고서를 저장했습니다 (연관 덤프 없음).\n{path}')

        def _worker():
            err = None
            saved = []
            analyzed = 0
            try:
                with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    # utf-8-sig(BOM): 메모장/Excel에서 한글이 깨지지 않게 한다
                    zf.writestr('crash_report.txt',
                                _make_report(ev).encode('utf-8-sig'))
                    for dmp in dumps:
                        zf.write(dmp, dmp.name)
                        saved.append(dmp.name)
                        report = self._analyze_dump_text(dmp)
                        if report:
                            zf.writestr(f'{dmp.name}.analysis.txt',
                                        report.encode('utf-8-sig'))
                            analyzed += 1
            except Exception as ex:
                err = ex
            # Tk 위젯 조작은 GUI 스레드에서만
            root = self._root
            if root is not None:
                root.after(0, lambda: _finish(err, saved, analyzed))

        threading.Thread(target=_worker, daemon=True).start()


# ── 메인 앱 ──────────────────────────────────────────────────────
class QAMonitor:
    def __init__(self):
        self._history: List[CrashEvent] = load_history()
        self._install_history: List[InstallEvent] = load_install_history()
        self._lock = threading.Lock()
        self._icon: Optional[pystray.Icon] = None
        # AUMID로 만들어야 _register_aumid()가 등록한 표시명/로고가 알림에
        # 나온다. 미지원 OS 등 생성 실패 시 None → _toast가 풍선으로 폴백.
        try:
            self._toaster = WindowsToaster(APP_AUMID) if HAS_WINDOWS_TOASTS else None
        except Exception:
            self._toaster = None
        self._watcher = EventLogWatcher(self._on_crash, self._on_install, self._on_poll,
                                        self._on_backfill_done, self._on_wer)
        self._dump_watcher = DumpWatcher(self._on_stack_trace)
        self._hang_watcher = HangWatcher(self._on_hang)
        # 최근 WER 부가 보고 (크래시가 나중에 도착하는 경우의 선-첨부용)
        self._recent_wer: deque = deque(maxlen=20)
        self._open_window: Optional[HistoryWindow] = None
        self._last_checked: Optional[datetime] = None
        self._consec_fail  = 0
        self._alert_showing = False

    def run(self):
        self._watcher.start()
        self._dump_watcher.start()
        self._hang_watcher.start()

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

    def _toast(self, title: str, msg: str, duration: int = 8,
               on_click: Optional[Callable] = None):
        """토스트 알림 공통 헬퍼. 클릭하면 on_click 실행(알림 센터 정식 지원).
        발송 실패 시 트레이 풍선 알림(_icon.notify)으로 폴백한다.
        show()는 즉시 반환(fire-and-forget)이라 스레드/직렬화가 필요 없다."""
        try:
            if self._toaster is None:
                raise RuntimeError('windows_toasts unavailable')
            toast = Toast(
                # 알림 센터 토스트는 텍스트 3줄 제한: 제목 + 본문 최대 2줄
                text_fields=[title, *msg.splitlines()[:2]],
                duration=(ToastDuration.Short if duration <= 5
                          else ToastDuration.Default),
            )
            if on_click is not None:
                toast.on_activated = lambda _args: on_click()
            self._toaster.show_toast(toast)
        except Exception:
            try:
                if self._icon:
                    self._icon.notify(msg, title=title)
            except Exception:
                pass

    def _notify_start(self):
        msg = 'ezFinder · ezCapture · ezCam · ezMemo · ezZip · ezManager 감시 중'
        self._toast('ezLab QA Monitor 시작됨', msg,
                    duration=5, on_click=self._show_history)

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
        self._toast('[이지랩 QA] 모니터링 오류', msg)

    @staticmethod
    def _append_wer_line(ev: CrashEvent, line: str) -> bool:
        """이력 상세에 연관 WER 보고 한 줄 추가 (섹션 생성/중복 방지).
        호출자가 _lock을 보유한 상태여야 한다."""
        if line in ev.detail:
            return False
        if WER_SECTION_TITLE in ev.detail:
            ev.detail += '\n' + line
        else:
            ev.detail += f'\n\n{WER_SECTION_TITLE}\n' + '─' * 48 + '\n' + line
        return True

    def _on_wer(self, ts: datetime, event_name: str, target_exe: str):
        """WER 부가 보고(1001) 수신: 근접한 기존 이력에 소급 첨부하고,
        크래시/행 이벤트가 아직 안 온 경우를 위해 최근 목록에도 보관한다."""
        label = WER_EVENT_LABELS.get(event_name)
        line  = (f'{ts.strftime("%Y-%m-%d %H:%M:%S")}  {event_name}'
                 + (f' — {label}' if label else ''))
        with self._lock:
            self._recent_wer.append((ts, target_exe.lower(), line))
            snapshot = None
            for ev in reversed(self._history):
                if (ev.process.lower() == target_exe.lower()
                        and abs((ev.timestamp - ts).total_seconds()) <= WER_CORRELATE_SEC):
                    if self._append_wer_line(ev, line):
                        snapshot = list(self._history)
                    break
        if snapshot is not None:
            save_history(snapshot)

    def _on_hang(self, exe: str, pid: int, hung_secs: float, dump_ok: bool):
        """HangWatcher 콜백: 응답 없음을 실시간으로 잡았을 때. 프로세스가 아직
        살아 있으므로 1002보다 먼저 이력에 남고, 캡처된 덤프는 DumpWatcher가
        분석해 스택을 이 이력에 자동 첨부한다."""
        ts  = datetime.now()
        sep = '─' * 48
        dump_path = DUMP_DIR / f'{exe}.{pid}.dmp'
        detail = (
            f'감지 방식   : 실시간 응답 없음 감시 (HangWatcher)\n'
            f'프로세스    : {exe}  (PID {pid})\n'
            f'감지 시각   : {ts.strftime("%Y-%m-%d %H:%M:%S")}\n'
            f'무응답 지속 : {int(hung_secs)}초 이상\n'
            f'덤프 캡처   : {"성공 — " + str(dump_path) if dump_ok else "실패"}\n'
            f'{sep}\n'
            'Windows가 AppHang(1002)으로 기록하기 전, 프로세스 생존 중에 선제\n'
            '감지한 이벤트입니다. 이후 사용자가 창을 닫으면 별도의 "응답 없음\n'
            '(Hang)" 이력이 추가로 남을 수 있습니다. 덤프가 캡처된 경우 관리\n'
            '코드 스택은 잠시 후 자동 분석되어 이 상세에 첨부됩니다.'
        )
        ev = CrashEvent(ts, self._watcher._resolve(exe), exe,
                        '응답 없음 감지 (실시간)',
                        f'UI 응답 없음 {int(hung_secs)}초 경과 — 종료 전 덤프 '
                        f'{"캡처 완료" if dump_ok else "캡처 실패"}', detail)
        self._on_crash(ev)

    def _on_crash(self, ev: CrashEvent, backfill: bool = False):
        with self._lock:
            # 먼저 도착해 있던 연관 WER 보고(RADAR 등)를 상세에 선-첨부
            for wts, wproc, wline in self._recent_wer:
                if (wproc == ev.process.lower()
                        and abs((ev.timestamp - wts).total_seconds()) <= WER_CORRELATE_SEC):
                    self._append_wer_line(ev, wline)
            self._history.append(ev)
            count = len(self._history)
            win = self._open_window
            # 백필 중에는 건마다 디스크에 쓰지 않고(수백 건이면 O(n²) I/O),
            # _on_backfill_done에서 한 번만 저장한다. watcher_state는 폴이
            # 끝난 뒤에 저장되므로 중간에 죽어도 다음 시작 때 다시 백필된다.
            snapshot = None if backfill else list(self._history)
        if snapshot is not None:
            save_history(snapshot)

        # 백필(미실행 중 발생분)은 이력에만 조용히 추가하고, 토스트/알림은
        # _on_backfill_done에서 합계로 한 번만 띄운다.
        if backfill:
            if win:
                win.push_event(ev)
            return

        self._alert_showing = True

        # 창이 열려 있으면 실시간으로 카드 추가
        if win:
            win.push_event(ev)

        if self._icon:
            self._icon.icon  = self._make_tray_icon(alert=True)
            self._icon.title = f'ezLab QA Monitor — {count}건 감지'

        msg = f'{ev.app_name}  |  {ev.error_type}\n{ev.summary[:100]}'
        self._toast('[이지랩 QA] 크래시 감지', msg, on_click=self._show_history)

    def _on_install(self, ev: InstallEvent):
        with self._lock:
            self._install_history.append(ev)
            win = self._open_window
            snapshot = list(self._install_history)
        save_install_history(snapshot)
        if win:
            win.push_install(ev)

    def _on_backfill_done(self, count: int):
        if count <= 0:
            return
        # 백필 동안 미뤄둔 크래시 이력 저장을 여기서 한 번에 수행
        with self._lock:
            snapshot = list(self._history)
        save_history(snapshot)
        self._alert_showing = True
        if self._icon:
            self._icon.icon  = self._make_tray_icon(alert=True)
            self._icon.title = f'ezLab QA Monitor — 미실행 중 발생분 {count}건 발견'
        msg = f'모니터가 꺼져 있던 동안 발생한 이벤트 {count}건을 이력에 추가했습니다.'
        self._toast('[이지랩 QA] 미실행 구간 이벤트 발견', msg,
                    on_click=self._show_history)

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
                # 이미 열려 있으면 새 창 대신 기존 창을 앞으로 (큐 적재만 하므로
                # 락 안에서 불러도 안전)
                self._open_window.bring_to_front()
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
    _enable_dpi_awareness()
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
