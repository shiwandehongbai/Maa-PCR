using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Media.Imaging;
using Avalonia.Threading;
using MFAAvalonia.Pcr;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

internal static class ClanGuidePerformanceChecks
{
    private sealed record Fixture(string Config, string Guide, JObject[] Jobs, Dictionary<string, string> Sources);
    private static readonly BindingFlags Hidden = BindingFlags.Instance | BindingFlags.NonPublic;

    public static void Run(string sourceRoot, string sourceConfigDirectory, string directory)
    {
        var root = Path.GetFullPath(Path.Combine(directory, "clan-performance"));
        var allowed = Path.GetFullPath(Path.Combine(sourceRoot, ".runtime")) + Path.DirectorySeparatorChar;
        if (!root.StartsWith(allowed, StringComparison.OrdinalIgnoreCase) || Directory.Exists(root))
            throw new IOException("Performance checks require a new isolated directory under this repository's .runtime.");
        Directory.CreateDirectory(root);
        var report = new JObject { ["version"] = 1, ["purpose"] = "offline real-controls performance with copied current three-team materials", ["game_input"] = false,
            ["real_config_written"] = false, ["rounds"] = 30, ["slot_loads"] = 90, ["forced_test_gc_excluded_from_latency"] = true };
        var latencies = new ConcurrentQueue<double>();
        var references = new List<WeakReference>();
        var memory = new List<(int Round, long Managed, int Images)>();
        var oldContext = SynchronizationContext.Current;
        Window? host = null;
        try
        {
            SynchronizationContext.SetSynchronizationContext(new AvaloniaSynchronizationContext());
            var fixture = Prepare(sourceRoot, sourceConfigDirectory, root);
            var view = new PcrClanGuideView(sourceRoot, fixture.Guide, fixture.Config);
            host = new Window { Width = 1120, Height = 840, ShowActivated = false, ShowInTaskbar = false,
                WindowStartupLocation = WindowStartupLocation.Manual, Position = new PixelPoint(-32000, -32000), Content = view };
            host.Show(); Wait(view); host.UpdateLayout();
            for (var cycle = 0; cycle < 3; cycle++)
                for (var slot = 1; slot <= 3; slot++) Exercise(view, host, slot, fixture.Jobs[slot - 1], references);
            memory.Add((0, RetainedMemory(), references.Count(reference => reference.IsAlive)));
            for (var batch = 0; batch < 3; batch++)
            {
                using (var heartbeat = new Heartbeat(latencies))
                    for (var round = batch * 10; round < (batch + 1) * 10; round++)
                        for (var slot = 1; slot <= 3; slot++) Exercise(view, host, slot, fixture.Jobs[slot - 1], references);
                memory.Add(((batch + 1) * 10, RetainedMemory(), references.Count(reference => reference.IsAlive)));
            }
            var samples = latencies.OrderBy(value => value).ToArray();
            if (samples.Length < 100) throw new InvalidOperationException("Insufficient dispatcher heartbeat samples.");
            var p95 = samples[(int)Math.Ceiling(samples.Length * .95) - 1];
            var maximum = samples[^1];
            var totalTolerance = Math.Max(32L * 1024 * 1024, memory[0].Managed / 10);
            var tailTolerance = Math.Max(8L * 1024 * 1024, memory[0].Managed / 50);
            report["samples"] = samples.Length; report["dispatcher_p95_ms"] = p95; report["dispatcher_max_ms"] = maximum;
            report["managed_total_growth_limit_bytes"] = totalTolerance; report["managed_last_ten_round_growth_limit_bytes"] = tailTolerance;
            report["memory_checkpoints"] = new JArray(memory.Select(point => new JObject { ["round"] = point.Round, ["retained_managed_bytes"] = point.Managed,
                ["retained_image_objects"] = point.Images }));
            report["image_objects_observed"] = references.Count;
            report["source_materials_unchanged"] = fixture.Sources.All(pair => PcrClanGuide.Stamp(pair.Key) == pair.Value);
            if ((bool)report["source_materials_unchanged"]! != true) throw new IOException("Original materials changed during the isolated check.");
            if (p95 > 100 || maximum > 500) throw new InvalidOperationException($"Dispatcher latency exceeded target: P95={p95:F2}ms, max={maximum:F2}ms.");
            if (memory[^1].Managed - memory[0].Managed > totalTolerance || memory[^1].Managed - memory[^2].Managed > tailTolerance)
                throw new InvalidOperationException("Retained managed memory did not reach a stable range after warmup.");
            if (memory[^1].Images > memory[0].Images + 2) throw new InvalidOperationException("Collapsed image objects accumulated across repeated loads.");
            report["status"] = "passed";
            Console.WriteLine($"CLAN_GUI_PERFORMANCE samples={samples.Length} p95_ms={p95:F2} max_ms={maximum:F2} retained_growth={memory[^1].Managed - memory[0].Managed}");
        }
        catch (Exception ex) { report["status"] = "failed"; report["error"] = ex.ToString(); throw; }
        finally
        {
            if (host != null) { host.Content = null; host.Close(); Dispatcher.UIThread.RunJobs(); }
            SynchronizationContext.SetSynchronizationContext(oldContext);
            File.WriteAllText(Path.Combine(root, "performance.json"), report.ToString(Formatting.Indented), new UTF8Encoding(false));
        }
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private static void Exercise(PcrClanGuideView view, Window host, int slot, JObject expected, List<WeakReference> references)
    {
        var load = view.LoadDailySlotAsync(slot); Wait(view, load);
        var selected = view.Capture().Job;
        if (!view.HasCurrentConfirmation || new[] { "boss", "unit_ids", "stars", "auto", "set_by_unit", "support_mode", "support_unit_id" }
                .Any(field => !JToken.DeepEquals(selected[field], expected[field])))
            throw new InvalidOperationException("Slot load changed the saved team, stars or controls, or lost its local confirmation.");
        var source = Field<Expander>(view, "sourceDetails"); source.IsExpanded = true; Wait(view);
        var image = Field<Image>(view, "image"); var preview = Field<Image>(view, "preview");
        if (image.Source == null || preview.Source == null) throw new InvalidOperationException("Expanded source images were not loaded.");
        references.Add(new WeakReference(image.Source)); references.Add(new WeakReference(preview.Source));
        source.IsExpanded = false; Wait(view);
        if (image.Source != null || preview.Source != null) throw new InvalidOperationException("Collapsed source retained its visible images.");
        var fields = Field<Dictionary<string, TextBox>>(view, "fields"); var label = fields["team_label"].Text;
        fields["team_label"].Text = label + " · performance check";
        if (view.HasCurrentConfirmation) throw new InvalidOperationException("An edited team remained executable.");
        fields["team_label"].Text = label;
        var scroll = (ScrollViewer)view.Content!;
        scroll.Offset = new Vector(0, Math.Max(0, scroll.Extent.Height - scroll.Viewport.Height)); host.UpdateLayout();
        scroll.Offset = default; host.UpdateLayout(); Pump(TimeSpan.FromMilliseconds(20));
    }

    private static void Wait(PcrClanGuideView view, Task? operation = null)
    {
        var clock = Stopwatch.StartNew(); var pending = view.PendingLoad;
        while (clock.Elapsed < TimeSpan.FromSeconds(20))
        {
            Dispatcher.UIThread.RunJobs();
            if (pending.IsCompleted && (operation?.IsCompleted ?? true))
            {
                pending.GetAwaiter().GetResult(); Dispatcher.UIThread.RunJobs(); pending = view.PendingLoad;
                if (pending.IsCompleted)
                {
                    pending.GetAwaiter().GetResult(); operation?.GetAwaiter().GetResult(); return;
                }
            }
            Thread.Sleep(1);
        }
        throw new TimeoutException("The latest asynchronous guide load did not finish.");
    }
    private static void Pump(TimeSpan duration)
    { var clock = Stopwatch.StartNew(); do { Dispatcher.UIThread.RunJobs(); Thread.Sleep(1); } while (clock.Elapsed < duration); }
    private static long RetainedMemory()
    {
        Dispatcher.UIThread.RunJobs(); GC.Collect(); GC.WaitForPendingFinalizers(); GC.Collect(); Dispatcher.UIThread.RunJobs();
        return GC.GetTotalMemory(false);
    }
    private static T Field<T>(object value, string name) => (T)value.GetType().GetField(name, Hidden)!.GetValue(value)!;

    private sealed class Heartbeat : IDisposable
    {
        private readonly ManualResetEventSlim stop = new();
        private readonly Thread worker;
        internal Heartbeat(ConcurrentQueue<double> samples)
        {
            var clock = Stopwatch.StartNew();
            worker = new Thread(() =>
            {
                var due = 10d;
                while (!stop.IsSet)
                {
                    var wait = due - clock.Elapsed.TotalMilliseconds;
                    if (wait > 0 && stop.Wait(TimeSpan.FromMilliseconds(wait))) break;
                    if (stop.IsSet) break;
                    var expected = due;
                    Dispatcher.UIThread.Post(() => samples.Enqueue(Math.Max(0, clock.Elapsed.TotalMilliseconds - expected)), DispatcherPriority.Input);
                    due = Math.Max(due + 10, clock.Elapsed.TotalMilliseconds + 10);
                }
            }) { IsBackground = false, Name = "clan-ui-dispatcher-heartbeat" };
            worker.Start();
        }
        public void Dispose()
        {
            stop.Set();
            if (!worker.Join(TimeSpan.FromSeconds(3))) throw new TimeoutException("Dispatcher heartbeat worker did not release.");
            Dispatcher.UIThread.RunJobs(); stop.Dispose();
        }
    }

    private static Fixture Prepare(string sourceRoot, string sourceConfigDirectory, string root)
    {
        var sourceConfig = Path.GetFullPath(sourceConfigDirectory);
        var config = Path.Combine(root, "config"); Directory.CreateDirectory(config);
        var sources = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        byte[] Read(string path)
        {
            path = Path.GetFullPath(path); var bytes = File.ReadAllBytes(path); var hash = PcrClanGuide.Hash(bytes);
            if (sources.TryGetValue(path, out var previous) && previous != hash) throw new IOException("Source changed while copying performance materials.");
            sources[path] = hash; return bytes;
        }
        string Copy(string path, string target) { var bytes = Read(path); Directory.CreateDirectory(Path.GetDirectoryName(target)!); File.WriteAllBytes(target, bytes); return target; }
        Copy(Path.Combine(sourceConfig, "guide-review.json"), Path.Combine(config, "guide-review.json"));
        var sourceGuide = Path.Combine(sourceConfig, "agent-guide-review.json"); var dailyPath = PcrClanDailyPlan.PathFor(sourceConfig);
        _ = Read(sourceGuide); _ = Read(dailyPath);
        var original = PcrClanDailyPlan.Read(dailyPath, out _);
        if (original.Slots.Count != 3) throw new InvalidDataException("Current material fixture must contain exactly three saved teams.");
        var guide = Path.Combine(config, "agent-guide-review.json");
        var saved = new Dictionary<string, PcrClanGuideSavedJob>(StringComparer.Ordinal);
        var daily = new PcrClanDailyPlanDocument(); var bosses = new int[3]; var jobs = new JObject[3];
        var bundles = new Dictionary<string, (string Path, string Hash)>(StringComparer.OrdinalIgnoreCase);
        var images = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        var materials = Path.Combine(root, "materials"); Directory.CreateDirectory(materials);
        string ImageCopy(string path)
        {
            path = Path.GetFullPath(path);
            if (!images.TryGetValue(path, out var copy))
            {
                var bytes = Read(path); copy = Path.Combine(materials, "image-" + PcrClanGuide.Hash(bytes) + Path.GetExtension(path));
                if (!File.Exists(copy)) File.WriteAllBytes(copy, bytes); images[path] = copy;
            }
            return copy;
        }
        foreach (var slot in original.Slots.OrderBy(value => value.Slot))
        {
            var document = PcrClanDailyPlan.Load(original, slot.Slot, sourceGuide, sourceConfig, sourceRoot, DateTimeOffset.Now, out _);
            if (!bundles.TryGetValue(document.BundlePath, out var copied))
            {
                var bytes = Read(document.BundlePath); var bundle = PcrClanGuide.Bundle(document.BundlePath);
                foreach (var job in ((JArray)bundle["jobs"]!).OfType<JObject>()) job["source"]!["image_path"] = ImageCopy((string)job["source"]!["image_path"]!);
                var path = Path.Combine(materials, "bundle-" + PcrClanGuide.Hash(bytes) + ".json");
                File.WriteAllText(path, bundle.ToString(Formatting.Indented), new UTF8Encoding(false));
                copied = (path, PcrClanGuide.Stamp(path)!); bundles[document.BundlePath] = copied;
            }
            document.BundlePath = copied.Path; document.BundleSha256 = copied.Hash;
            document.Job["source"]!["image_path"] = ImageCopy((string)document.Job["source"]!["image_path"]!);
            var relative = Path.GetRelativePath(sourceConfig, document.SnapshotPath);
            if (Path.IsPathRooted(relative) || relative.Split(Path.DirectorySeparatorChar).Any(part => part == "..")) throw new IOException("Account snapshot is outside the source config.");
            document.SnapshotPath = Copy(document.SnapshotPath, Path.Combine(config, relative));
            document.SavedJobs = saved; document.ConfirmedFor = null;
            _ = PcrClanGuide.Save(document, guide, PcrClanGuide.Stamp(guide), config, sourceRoot, DateTimeOffset.Now, true);
            PcrClanDailyPlan.Assign(daily, slot.Slot, document, guide, config, sourceRoot, DateTimeOffset.Now);
            bosses[slot.Slot - 1] = (int)document.Job["boss"]!; jobs[slot.Slot - 1] = (JObject)document.Job.DeepClone();
            if (bosses[slot.Slot - 1] == 4)
            {
                var plan = PcrClanGuide.BuildPlan(document, config, sourceRoot, DateTimeOffset.Now);
                var index = ((JArray)plan["unit_ids"]!).Values<int>().ToList().IndexOf(113801);
                if (index < 0 || (int)plan["stars"]![index]! != 4) throw new InvalidDataException("The accepted four-star angel Yori setting changed.");
            }
        }
        PcrClanDailyPlan.Save(daily, PcrClanDailyPlan.PathFor(config), null);
        var execution = PcrClanDailyExecution.PathFor(sourceConfig);
        if (File.Exists(execution)) Copy(execution, PcrClanDailyExecution.PathFor(config));
        var state = PcrClanDailyExecution.Read(PcrClanDailyExecution.PathFor(config), out _);
        if (state.Mode != PcrClanDailyExecution.Simulation || state.BattleSpeed != 2) throw new InvalidDataException("Performance fixture must retain simulation at two speed.");
        if (!bosses.SequenceEqual(new[] { 5, 4, 1 })) throw new InvalidDataException("Current accepted fixture must preserve D5, D4, D1.");
        File.WriteAllText(Path.Combine(root, "fixture-materials.json"), new JObject { ["copied_current_materials"] = true, ["game_evidence"] = false,
            ["original_files"] = JObject.FromObject(sources), ["bosses"] = new JArray(bosses) }.ToString(Formatting.Indented), new UTF8Encoding(false));
        return new(config, guide, jobs, sources);
    }
}
