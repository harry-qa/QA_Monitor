// 덤프 캡처 + ClrMD 스택 트레이스 추출 파이프라인 검증용 합성 크래시 프로그램.
// 실제 ezFinderService 대신 이걸로 AccessViolationException을 재현한다.
Console.WriteLine("CrashTest starting, PID=" + Environment.ProcessId);
Level1();

static void Level1() => Level2();
static void Level2() => Level3();

static unsafe void Level3()
{
    Console.WriteLine("Level3 reached, sleeping 15s so a dump can be collected mid-stack...");
    Thread.Sleep(15000);
    byte* p = (byte*)0x1;
    *p = 1; // 의도적 메모리 접근 위반 -> AccessViolationException (native crash)
}
