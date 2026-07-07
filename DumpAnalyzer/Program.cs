using Microsoft.Diagnostics.Runtime;
using System.Text;

// 한글 리포트가 호출자(파이썬 subprocess, UTF-8로 읽음)에게 깨지지 않도록
// 콘솔 출력 인코딩을 BOM 없는 UTF-8로 고정한다. (기본값은 시스템 OEM
// 코드페이지 cp949라 파이썬이 UTF-8로 읽으면 한글이 깨짐)
Console.OutputEncoding = new UTF8Encoding(encoderShouldEmitUTF8Identifier: false);

if (args.Length < 1)
{
    Console.Error.WriteLine("Usage: DumpAnalyzer <dump-file-path> [--full]");
    return 1;
}

string dumpPath = args[0];
// --full: QA/개발자 전달용 사람이 읽는 전체 리포트(모든 스레드 스택 + 예외 +
// 모듈 + 힙 통계). 인자 없으면 기존 동작(크래시 스레드 1개 스택)으로, 이력에
// 자동 첨부되는 경량 경로를 그대로 유지한다.
bool full = args.Any(a => string.Equals(a, "--full", StringComparison.OrdinalIgnoreCase));

try
{
    using DataTarget dataTarget = DataTarget.LoadDump(dumpPath);
    ClrInfo? clrInfo = dataTarget.ClrVersions.FirstOrDefault();
    if (clrInfo == null)
    {
        Console.WriteLine("NO_CLR_RUNTIME_FOUND");
        return 0;
    }

    using ClrRuntime runtime = clrInfo.CreateRuntime();

    if (full)
    {
        Console.WriteLine(BuildFullReport(dumpPath, dataTarget, runtime));
        return 0;
    }

    ClrThread? thread = runtime.Threads.FirstOrDefault(t => t.CurrentException != null)
                      ?? runtime.Threads.FirstOrDefault(t => t.IsAlive && t.EnumerateStackTrace().Any());

    if (thread == null)
    {
        Console.WriteLine("NO_MANAGED_THREAD_FOUND");
        return 0;
    }

    var sb = new StringBuilder();

    ClrException? ex = thread.CurrentException;
    if (ex != null)
    {
        sb.AppendLine($"Exception: {ex.Type?.Name}");
        sb.AppendLine($"Message: {ex.Message}");
    }

    sb.AppendLine("StackTrace:");
    foreach (ClrStackFrame frame in thread.EnumerateStackTrace())
    {
        string sig = frame.Method?.Signature ?? frame.ToString() ?? "<unknown>";
        sb.AppendLine($"  at {sig}");
    }

    Console.WriteLine(sb.ToString());
    return 0;
}
catch (Exception ex)
{
    Console.WriteLine($"ANALYZER_ERROR: {ex.Message}");
    return 1;
}


static string BuildFullReport(string dumpPath, DataTarget dataTarget, ClrRuntime runtime)
{
    var sb = new StringBuilder();
    string bar = new string('=', 70);
    string sep = new string('-', 70);

    sb.AppendLine("[이지랩 QA 덤프 분석 리포트]");
    sb.AppendLine(bar);
    sb.AppendLine($"덤프 파일   : {Path.GetFileName(dumpPath)}");
    try { sb.AppendLine($"생성 시각   : {File.GetLastWriteTime(dumpPath):yyyy-MM-dd HH:mm:ss}"); }
    catch { /* 파일 시간 조회 실패는 무시 */ }
    sb.AppendLine($".NET 런타임 : {runtime.ClrInfo.Version}  ({runtime.ClrInfo.Flavor})");
    sb.AppendLine(bar);
    sb.AppendLine();
    sb.AppendLine("※ 이 리포트는 덤프를 뜬 '그 순간'의 정지 화면입니다 — 크래시/멈춤 지점은");
    sb.AppendLine("   정확하지만, 그 이전 조작 전체가 기록된 것은 아닙니다.");
    sb.AppendLine();

    // ── 예외 (있으면) ──
    var exThreads = runtime.Threads.Where(t => t.CurrentException != null).ToList();
    sb.AppendLine("[예외 정보]");
    sb.AppendLine(sep);
    if (exThreads.Count == 0)
    {
        sb.AppendLine("활성 관리 예외 없음 — 예외로 죽은 크래시가 아니라 멈춤(행)이거나");
        sb.AppendLine("네이티브 코드 크래시일 수 있습니다. 아래 스레드 스택에서 위치를 확인하세요.");
    }
    else
    {
        foreach (ClrThread t in exThreads)
        {
            ClrException ex = t.CurrentException!;
            sb.AppendLine($"스레드 OSID 0x{t.OSThreadId:x} 에서 예외 발생:");
            for (ClrException? cur = ex; cur != null; cur = cur.Inner)
            {
                sb.AppendLine($"  형식    : {cur.Type?.Name}");
                sb.AppendLine($"  메시지  : {cur.Message}");
                sb.AppendLine($"  HRESULT : 0x{cur.HResult:x8}");
                if (cur.Inner != null)
                    sb.AppendLine("  --- 내부 예외(inner) ---");
            }
        }
    }
    sb.AppendLine();

    // ── 스레드별 관리 코드 스택 ──
    sb.AppendLine("[스레드별 관리 코드 스택]");
    sb.AppendLine(sep);
    int shown = 0;
    foreach (ClrThread t in runtime.Threads)
    {
        var frames = t.EnumerateStackTrace()
                      .Where(f => f.Kind == ClrStackFrameKind.ManagedMethod || f.Method != null)
                      .ToList();
        if (frames.Count == 0)
            continue;   // 관리 프레임이 없는 스레드(순수 네이티브/대기)는 생략

        shown++;
        string tags = string.Join(" ", new[]
        {
            t.IsFinalizer ? "[Finalizer]" : null,
            t.CurrentException != null ? "[예외]" : null,
        }.Where(x => x != null));
        sb.AppendLine($"── 스레드 #{t.ManagedThreadId}  (OSID 0x{t.OSThreadId:x}) {tags}".TrimEnd());
        foreach (ClrStackFrame frame in frames)
        {
            string sig = frame.Method?.Signature ?? frame.FrameName ?? "<unknown>";
            sb.AppendLine($"    at {sig}");
        }
        sb.AppendLine();
    }
    if (shown == 0)
        sb.AppendLine("관리 코드 스택을 가진 스레드가 없습니다.");
    sb.AppendLine();

    // ── 힙 통계 (형식별 개수/크기 상위 40) ──
    sb.AppendLine("[힙 통계 — 메모리를 많이 차지한 형식 상위 40]");
    sb.AppendLine(sep);
    try
    {
        var stats = new Dictionary<string, (int Count, ulong Size)>();
        ulong total = 0;
        foreach (ClrObject obj in runtime.Heap.EnumerateObjects())
        {
            if (obj.Type == null) continue;
            string name = obj.Type.Name ?? "<unknown>";
            ulong size = obj.Size;
            total += size;
            if (stats.TryGetValue(name, out var e))
                stats[name] = (e.Count + 1, e.Size + size);
            else
                stats[name] = (1, size);
        }
        sb.AppendLine($"{"크기(MB)",10}  {"개수",10}  형식");
        foreach (var kv in stats.OrderByDescending(k => k.Value.Size).Take(40))
        {
            double mb = kv.Value.Size / 1024.0 / 1024.0;
            string name = kv.Key.Length > 80 ? kv.Key.Substring(0, 77) + "..." : kv.Key;
            sb.AppendLine($"{mb,10:F2}  {kv.Value.Count,10:N0}  {name}");
        }
        sb.AppendLine(sep);
        sb.AppendLine($"관리 힙 총합: {total / 1024.0 / 1024.0:F1} MB, 형식 종류 {stats.Count:N0}개");
    }
    catch (Exception hx)
    {
        sb.AppendLine($"(힙 통계 수집 실패: {hx.Message})");
    }
    sb.AppendLine();

    // ── 로드된 모듈 ──
    sb.AppendLine("[로드된 모듈]");
    sb.AppendLine(sep);
    try
    {
        foreach (ClrModule mod in runtime.EnumerateModules().OrderBy(m => m.Name))
        {
            string name = mod.Name ?? "<dynamic>";
            sb.AppendLine($"  {name}");
        }
    }
    catch (Exception mx)
    {
        sb.AppendLine($"(모듈 목록 수집 실패: {mx.Message})");
    }

    return sb.ToString();
}
