using Microsoft.Diagnostics.Runtime;
using System.Text;

if (args.Length < 1)
{
    Console.Error.WriteLine("Usage: DumpAnalyzer <dump-file-path>");
    return 1;
}

string dumpPath = args[0];

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
