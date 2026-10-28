using System;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Tasks;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.Pcr;
using Newtonsoft.Json.Linq;

internal static class AgentConnectionMonitorChecks
{
    private const BindingFlags Hidden = BindingFlags.Instance | BindingFlags.Static | BindingFlags.NonPublic;
    private static ManualResetEventSlim entered = null!, release = null!, finished = null!;
    private static int reads, pid, localPort;
    private static bool blocked, absent;

    internal static void Run(string[] args)
    {
        var owner = typeof(PcrAlphaOwner);
        var assembly = owner.Assembly;
        var processType = assembly.GetType("MFAAvalonia.Pcr.PcrWindowsChild", true)!;
        var childType = owner.GetNestedType("ChildSession", BindingFlags.NonPublic)!;
        var sessionType = owner.GetNestedType("Session", BindingFlags.NonPublic)!;
        var configType = owner.GetNestedType("ValidatedConfig", BindingFlags.NonPublic)!;
        var connection = assembly.GetType("MFAAvalonia.Pcr.PcrAgentConnection", true)!;
        var query = connection.GetField("readTable", Hidden)!;
        var original = query.GetValue(null);
        var current = owner.GetField("current", Hidden)!;
        var previous = current.GetValue(null);
        var count = 0;
        void Require(bool value, string check)
        { if (!value) throw new InvalidOperationException(check); count++; }
        object Construct(Type target, params object?[] values) => Activator.CreateInstance(target,
            BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic, null, values, null)!;
        Require(previous == null, "connection_monitor_fixture_requires_idle_owner");
        try
        {
            query.SetValue(null, Delegate.CreateDelegate(query.FieldType,
                typeof(AgentConnectionMonitorChecks).GetMethod(nameof(ReadTable), Hidden)!));
            foreach (var scenario in new[] { "slow_startup", "slow_monitor_cancel", "startup_cancel", "absent", "changed" })
            {
                using var enteredEvent = new ManualResetEventSlim();
                using var releaseEvent = new ManualResetEventSlim();
                using var finishedEvent = new ManualResetEventSlim();
                entered = enteredEvent; release = releaseEvent; finished = finishedEvent;
                reads = 0; blocked = scenario.StartsWith("slow", StringComparison.Ordinal) || scenario == "startup_cancel";
                absent = scenario == "absent"; localPort = scenario == "changed" ? 790 : 789;
                using var process = (IDisposable)Activator.CreateInstance(processType, nonPublic: true)!;
                processType.GetMethod("Start", Hidden)!.Invoke(process, [Path.GetFullPath(args[2]),
                    Path.GetFullPath(args[1]), new[] { "-c", "import sys; sys.stdin.readline()" }]);
                pid = (int)processType.GetProperty("Id", Hidden)!.GetValue(process)!;
                var processor = (MaaProcessor)RuntimeHelpers.GetUninitializedObject(typeof(MaaProcessor));
                var session = Construct(sessionType, processor, new JObject(), Construct(configType, "{}"), new PcrStartConsent("", "", [], false));
                var cancel = (CancellationTokenSource)sessionType.GetField("Cancel")!.GetValue(session)!;
                var messages = new ConcurrentQueue<string>();
                var child = RuntimeHelpers.GetUninitializedObject(childType);
                void Set(string name, object value) => childType.GetField(name, Hidden)!.SetValue(child, value);
                Set("process", process); Set("session", session); Set("exitGate", new object());
                Set("shutdown", Activator.CreateInstance(assembly.GetType("MFAAvalonia.Pcr.PcrAgentShutdown", true)!, nonPublic: true)!);
                Set("request", new PlatformAgentStartRequest("456", "", [], args[1], "offline", "offline", messages.Enqueue));
                sessionType.GetField("Child")!.SetValue(session, child);
                current.SetValue(null, session);
                Task? work = null;
                try
                {
                    if (scenario is "slow_startup" or "startup_cancel")
                        work = (Task)childType.GetMethod("ReadConnectionAsync", Hidden)!.Invoke(child, null)!;
                    else
                    {
                        var peer = Construct(connection.GetNestedType("Peer", BindingFlags.NonPublic)!, pid, 789);
                        work = (Task)childType.GetMethod("WatchConnectionAsync", Hidden)!.Invoke(child, [peer])!;
                    }
                    Require(entered.Wait(TimeSpan.FromSeconds(5)), scenario + "_single_probe_entered");
                    if (scenario.StartsWith("slow", StringComparison.Ordinal))
                    {
                        Thread.Sleep(2200);
                        Require(!work.IsCompleted && reads == 1, scenario + "_over_two_seconds_is_not_disconnect_or_second_probe");
                        Require(!(bool)sessionType.GetField("Revoked")!.GetValue(session)!, scenario + "_does_not_revoke_live_owner");
                    }
                    if (scenario == "slow_startup")
                    {
                        release.Set(); work.WaitAsync(TimeSpan.FromSeconds(3)).GetAwaiter().GetResult();
                        var peer = work.GetType().GetProperty("Result")!.GetValue(work)!;
                        Require((int)peer.GetType().GetProperty("Port")!.GetValue(peer)! == 789, "slow_startup_returns_actual_same_peer");
                    }
                    else if (scenario is "slow_monitor_cancel" or "startup_cancel")
                    {
                        var clock = Stopwatch.StartNew(); cancel.Cancel();
                        try { work.WaitAsync(TimeSpan.FromSeconds(1)).GetAwaiter().GetResult(); }
                        catch (OperationCanceledException) when (scenario == "startup_cancel") { }
                        Require(clock.Elapsed < TimeSpan.FromSeconds(1), scenario + "_stop_does_not_wait_for_blocked_table");
                        ((IDisposable)child).Dispose(); release.Set();
                        Require(finished.Wait(TimeSpan.FromSeconds(3)), scenario + "_late_query_released_after_dispose");
                        Require((bool)sessionType.GetField("Revoked")!.GetValue(session)! && reads == 1,
                            scenario + "_late_result_cannot_restore_owner_or_dispatch_again");
                        if (scenario == "startup_cancel") Require(work.IsCanceled, "startup_cancel_remains_canceled_after_late_valid_row");
                    }
                    else
                    {
                        work.WaitAsync(TimeSpan.FromSeconds(5)).GetAwaiter().GetResult();
                        var cleanup = (Task?)childType.GetField("faultCleanup", Hidden)!.GetValue(child);
                        Require(cleanup != null, scenario + "_confirmed_fault_starts_shutdown");
                        cleanup!.WaitAsync(TimeSpan.FromSeconds(10)).GetAwaiter().GetResult();
                        Require((bool)sessionType.GetField("Revoked")!.GetValue(session)! && cancel.IsCancellationRequested,
                            scenario + "_confirmed_fault_revokes_owner");
                        Require(messages.TryPeek(out var reason) && reason.Contains(scenario == "absent" ? "connection is absent" : "connection changed", StringComparison.Ordinal),
                            scenario + "_reports_actual_connection_fault");
                    }
                }
                finally
                {
                    cancel.Cancel(); release.Set();
                    if (work != null) { try { work.WaitAsync(TimeSpan.FromSeconds(5)).GetAwaiter().GetResult(); } catch (Exception error) when (error is OperationCanceledException or TimeoutException) { } }
                    if (blocked && reads != 0 && !finished.Wait(TimeSpan.FromSeconds(3))) throw new TimeoutException("offline_late_probe_cleanup");
                    ((IDisposable)child).Dispose();
                    current.SetValue(null, previous); cancel.Dispose();
                }
            }
        }
        finally { query.SetValue(null, original); current.SetValue(null, previous); }
        Console.WriteLine($"PCR Agent connection monitor checks passed: {count}; slow same Peer, cancellation, late Dispose, absent/changed faults; no device/game input");
    }

    private static uint ReadTable(IntPtr table, ref uint size, bool order, int family, int tableClass, uint reserved)
    {
        size = 28;
        if (table == IntPtr.Zero)
        {
            Interlocked.Increment(ref reads); entered.Set();
            if (blocked && !release.Wait(TimeSpan.FromSeconds(10))) throw new TimeoutException("offline_probe_release");
            return 122;
        }
        var values = absent ? new int[] { 0 } : [1, 5, 0x0100007f, NetworkPort(localPort), 0x0100007f, NetworkPort(456), pid];
        for (var index = 0; index < values.Length; index++) Marshal.WriteInt32(table, index * 4, values[index]);
        finished.Set(); return 0;
    }
    private static int NetworkPort(int port) => (port & 255) << 8 | port >> 8;
}
