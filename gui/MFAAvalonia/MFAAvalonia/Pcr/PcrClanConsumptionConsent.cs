using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Threading;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using SukiUI.Controls;

namespace MFAAvalonia.Pcr;

internal sealed record PcrClanCodeBinding(string Source, string Calibration, string Core)
{
    internal static string FilesHash(string root, IEnumerable<string> names)
    {
        var text = new StringBuilder();
        foreach (var name in names.Distinct(StringComparer.Ordinal).Order(StringComparer.Ordinal))
            text.Append(name).Append('\0').Append(PcrClanGuide.Hash(File.ReadAllBytes(Local(root, name)))).Append('\n');
        return PcrClanGuide.Hash(Encoding.UTF8.GetBytes(text.ToString()));
    }
    internal static string Local(string root, string relative)
    {
        if (string.IsNullOrEmpty(relative) || Path.IsPathFullyQualified(relative) || relative.Contains('\\') || relative.Contains(':')
            || relative.Split('/').Any(part => part is "" or "." or "..")) throw new InvalidDataException("clan_consent_source_path_invalid");
        var home = Path.GetFullPath(root); var path = Path.GetFullPath(relative, home);
        if (!path.StartsWith(Path.TrimEndingDirectorySeparator(home) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("clan_consent_source_path_invalid");
        for (var cursor = new FileInfo(path) as FileSystemInfo; cursor != null; cursor = cursor is DirectoryInfo d ? d.Parent : ((FileInfo)cursor).Directory)
        {
            if ((cursor.Attributes & FileAttributes.ReparsePoint) != 0) throw new InvalidDataException("clan_consent_source_link_forbidden");
            if (string.Equals(cursor.FullName, home, StringComparison.OrdinalIgnoreCase)) break;
        }
        return path;
    }
    internal static string SourceHash(string root)
    {
        var names = new List<string>(); var pending = new Stack<string>(); pending.Push(Local(root, "src/pcr_maa"));
        while (pending.Count != 0)
            foreach (var path in Directory.EnumerateFileSystemEntries(pending.Pop()))
            {
                if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0) throw new InvalidDataException("clan_consent_source_link_forbidden");
                if (Directory.Exists(path)) pending.Push(path);
                else if (Path.GetExtension(path) == ".py") names.Add(Path.GetRelativePath(root, path).Replace('\\', '/'));
            }
        if (names.Count == 0) throw new InvalidDataException("clan_consent_source_missing");
        return FilesHash(root, names);
    }
    internal static string CalibrationHash(string root, JObject plans)
    {
        var names = new HashSet<string>(StringComparer.Ordinal) { PcrClanCalibration.IndexPath };
        var index = PcrClanGuide.ReadJson(Local(root, PcrClanCalibration.IndexPath));
        if (PcrClanGuide.Stamp(Local(root, PcrClanCalibration.IndexPath)) != PcrClanCalibration.IndexSha256)
            throw new InvalidDataException("clan_consent_calibration_index_changed");
        foreach (var version in plans.Properties().Select(p => (string?)p.Value["content_version"]).Distinct(StringComparer.Ordinal))
        {
            var checkedProfile = PcrClanCalibration.Read(root, version);
            if (checkedProfile.Error != null) throw new InvalidDataException(checkedProfile.Error);
            var entry = ((JArray)index["entries"]!).OfType<JObject>().Single(e => (string?)e["content_version"] == version);
            void Add(string name, string expected)
            {
                if (PcrClanGuide.Stamp(Local(root, name)) != expected) throw new InvalidDataException("clan_consent_calibration_changed");
                names.Add(name);
            }
            var profileName = (string)entry["profile_path"]!; Add(profileName, (string)entry["profile_sha256"]!);
            var profile = PcrClanGuide.ReadJson(Local(root, profileName));
            foreach (var samples in profile.DescendantsAndSelf().OfType<JObject>().Where(value => value["samples"] is JArray))
                foreach (var sample in (JArray)samples["samples"]!) Add((string)sample["path"]!, (string)sample["sha256"]!);
            foreach (var node in profile.DescendantsAndSelf().OfType<JObject>().Where(value => (string?)value["recognition"] == "TemplateMatch"))
            {
                var templates = node["template"] as JArray ?? new JArray(node["template"]!.DeepClone());
                var hashes = node["sha256"] as JArray ?? new JArray(node["sha256"]!.DeepClone());
                for (var i = 0; i < templates.Count; i++) Add("assets/resource/image/" + (string)templates[i], (string)hashes[i]);
            }
            if (((JArray)entry["capabilities"]!).Values<string>().Contains("timed") && profile["timing_measurements"] is JObject timing)
                Add((string)timing["path"]!, (string)timing["sha256"]!);
        }
        return FilesHash(root, names);
    }
    internal static string CoreHash() => PcrClanGuide.Hash(File.ReadAllBytes(typeof(PcrAlphaOwner).Assembly.Location));
    internal static PcrClanCodeBinding Capture(string root, JObject config) => new(SourceHash(root),
        CalibrationHash(root, (JObject)config["clan_battle_daily"]!["slot_plans"]!), CoreHash());
}

internal static class PcrClanConsumptionConsent
{
    internal const string Prefix = "pcr-clan-consent:";
    internal sealed record Preview(string Kind, int Count, string Text, DateTimeOffset Deadline)
    {
        internal int? SelectedSlot { get; set; }
    }
    private static bool Keys(JObject row, params string[] keys) => row.Properties().Select(p => p.Name).ToHashSet(StringComparer.Ordinal).SetEquals(keys);
    private static bool Integer(JToken? token, long low, long high) => token?.Type == JTokenType.Integer
        && long.TryParse(token.ToString(), NumberStyles.Integer, CultureInfo.InvariantCulture, out var value) && value >= low && value <= high;
    private static bool Digest(JToken? token) => token?.Type == JTokenType.String && Regex.IsMatch((string)token, "\\A[0-9a-f]{64}\\z");
    private static bool Nonce(JToken? token) => token?.Type == JTokenType.String && Regex.IsMatch((string)token, "\\A[0-9a-f]{32}\\z");
    private static string Text(JToken? token, int max = 200)
    {
        if (token?.Type != JTokenType.String || string.IsNullOrWhiteSpace((string?)token) || ((string)token).Length > max || ((string)token).Any(char.IsControl))
            throw new InvalidDataException("clan_consent_text_invalid");
        return (string)token;
    }
    internal static JObject Parse(string text)
    {
        if (text.Length > 65536) throw new InvalidDataException("clan_consent_request_too_large");
        using var reader = new JsonTextReader(new StringReader(text)) { DateParseHandling = DateParseHandling.None };
        var value = JObject.Load(reader, new JsonLoadSettings { DuplicatePropertyNameHandling = DuplicatePropertyNameHandling.Error });
        if (reader.Read()) throw new InvalidDataException("clan_consent_trailing_json");
        return value;
    }
    internal static Preview Describe(JObject request, JObject config, PcrClanCodeBinding code, DateTimeOffset now)
    {
        if (!Keys(request, "kind", "request_id", "root_task_id", "controller_uuid", "invocation_id", "daily_plan_hash", "timeout_seconds", "preview")
            || (string?)request["kind"] != "clan_consumption" || !Nonce(request["request_id"])
            || !Integer(request["root_task_id"], 1, long.MaxValue) || !Nonce(request["invocation_id"])
            || !Digest(request["daily_plan_hash"]) || (int?)request["timeout_seconds"] != 120 || request["preview"] is not JObject preview
            || !Keys(preview, "server", "account_id", "period", "game_day", "batch_id", "source_sha256", "calibration_sha256", "owner_core_sha256", "kind", "max_count", "observed_at", "remaining_count", "cp", "actions"))
            throw new InvalidDataException("clan_consent_preview_invalid");
        _ = Text(request["controller_uuid"]);
        if (config["clan_battle_daily"] is not JObject daily || (string?)daily["confirmed_for"] != (string?)request["daily_plan_hash"]
            || daily["slot_plans"] is not JObject plans || !Keys(plans, "1", "2", "3")
            || (string?)preview["source_sha256"] != code.Source || (string?)preview["calibration_sha256"] != code.Calibration
            || (string?)preview["owner_core_sha256"] != code.Core || !Digest(preview["batch_id"]))
            throw new InvalidDataException("clan_consent_binding_mismatch");
        var first = (JObject)plans["1"]!;
        if (new[] { "server", "account_id", "period" }.Any(key => !JToken.DeepEquals(preview[key], first[key]))
            || (string?)preview["server"] != "cn-bilibili" || !Regex.IsMatch(Text(preview["account_id"]), "\\A[1-9][0-9]{0,19}\\z")
            || !Regex.IsMatch(Text(preview["period"]), "\\A[0-9]{4}-(?:0[1-9]|1[0-2])\\z")
            || (string?)preview["game_day"] != now.ToOffset(TimeSpan.FromHours(8)).AddHours(-5).ToString("yyyy-MM-dd", CultureInfo.InvariantCulture))
            throw new InvalidDataException("clan_consent_account_or_day_mismatch");
        var binding = new JObject { ["server"] = preview["server"]!.DeepClone(), ["account_id"] = preview["account_id"]!.DeepClone(),
            ["period"] = preview["period"]!.DeepClone(), ["game_day"] = preview["game_day"]!.DeepClone(),
            ["daily_plan_hash"] = request["daily_plan_hash"]!.DeepClone(),
            ["slot_confirmations"] = new JObject(plans.Properties().Select(p => new JProperty(p.Name, p.Value["confirmed_for"]!.DeepClone()))) };
        if ((string?)preview["batch_id"] != PcrClanGuide.Fingerprint(binding)) throw new InvalidDataException("clan_consent_batch_mismatch");
        var observedText = Text(preview["observed_at"]);
        if (!Regex.IsMatch(observedText, "(?:Z|[+-][0-9]{2}:[0-9]{2})$")
            || !DateTimeOffset.TryParse(observedText, CultureInfo.InvariantCulture, DateTimeStyles.None, out var observed)
            || now < observed || now - observed > TimeSpan.FromSeconds(60)) throw new InvalidDataException("clan_consent_observation_expired");
        var kind = (string?)preview["kind"];
        var external = kind == "external_regular_baseline";
        if (kind is not ("regular" or "carryover" or "external_regular_baseline") || preview["actions"] is not JArray { Count: >= 1 and <= 3 } actions
            || !Integer(preview["max_count"], 1, 3) || !Integer(preview["remaining_count"], 0, 3) || !Integer(preview["cp"], 0, 900)
            || (external ? (int)preview["max_count"]! != 1 || actions.Count != 3 || (int)preview["cp"]! != 900
                    || (int)preview["remaining_count"]! != 2
                : (int)preview["max_count"]! != actions.Count)
            || kind == "regular" && actions.Count > (int)preview["remaining_count"]! || kind == "carryover" && actions.Count != 1)
            throw new InvalidDataException("clan_consent_count_invalid");
        var text = new StringBuilder($"账号 {preview["account_id"]} · {preview["period"]} · 游戏日 {preview["game_day"]}\n当前普通剩余 {preview["remaining_count"]} 次，CP {preview["cp"]}。\n");
        text.Append(external ? "请选择今天已在程序外使用的一支队伍。仅登记已用队伍，不授权消费。\n"
            : kind == "regular" ? $"本次只授权以下 {actions.Count} 次普通挑战；补偿另行确认。\n" : "本次只授权以下 1 次补偿；不新增普通挑战许可。\n");
        var slots = new HashSet<int>();
        foreach (var token in actions)
        {
            if (token is not JObject action || !Keys(action, "kind", "slot", "stock_index", "boss", "round", "phase", "instance_id", "start_seconds", "source_intent_id", "source_instance_id", "using_unit", "support_unit", "support_owner_viewer_id")
                || (string?)action["kind"] != (external ? "regular" : kind) || !Integer(action["slot"], 1, 3) || !slots.Add((int)action["slot"]!)
                || !Integer(action["boss"], 1, 5) || !Integer(action["round"], 1, 1000000) || !Integer(action["start_seconds"], 1, 90)
                || action["using_unit"] is not JArray { Count: 5 } units || units.Any(uid => !Integer(uid, 1, 100000000)) || units.Values<int>().Distinct().Count() != 5
                || action["support_unit"]!.Type != JTokenType.Null || action["support_owner_viewer_id"]!.Type != JTokenType.Null)
                throw new InvalidDataException("clan_consent_action_invalid");
            var job = (JObject)plans[action["slot"]!.ToString()]!;
            var phase = Text(action["phase"]); var instance = Text(action["instance_id"]);
            bool ownUnits;
            try { ownUnits = PcrClanCalibration.UsesOwnUnits(job); }
            catch (Exception ex) when (ex is IOException or InvalidOperationException or ArgumentException)
            { throw new InvalidDataException("clan_consent_support_snapshot_invalid", ex); }
            if (!ownUnits || !JToken.DeepEquals(action["boss"], job["boss"])
                || !units.Values<int>().Order().SequenceEqual(job["unit_ids"]!.Values<int>().Order())
                || phase != (string?)job["phase"] && !((string?)job["phase"] == "BC" && phase is "B" or "C" or "BC")
                || (long)action["round"]! < (long)job["round_min"]! || job["round_max"]?.Type == JTokenType.Integer && (long)action["round"]! > (long)job["round_max"]!)
                throw new InvalidDataException("clan_consent_job_mismatch");
            if (kind != "carryover" ? action["stock_index"]!.Type != JTokenType.Null || action["source_intent_id"]!.Type != JTokenType.Null
                    || action["source_instance_id"]!.Type != JTokenType.Null || (int)action["start_seconds"]! != 90
                : !Integer(action["stock_index"], 0, 1000000) || !Digest(action["source_intent_id"]) || action["source_instance_id"]!.Type != JTokenType.String
                    || Text(action["source_instance_id"]) == instance)
                throw new InvalidDataException("clan_consent_consumption_mode_mismatch");
            text.Append($"\n第 {action["slot"]} 刀 · {phase} 阶段 · BOSS {action["boss"]} · 第 {action["round"]} 轮\n具体实例：{instance}\n队伍：{string.Join(" / ", units.Values<int>())}\n");
            if (kind == "carryover") text.Append($"补偿库存 {action["stock_index"]} · {action["start_seconds"]} 秒\n来源主刀：{action["source_intent_id"]}\n来源实例：{action["source_instance_id"]}\n");
        }
        text.Append(external ? "\n仅登记今天程序外已用队伍，不记为程序结算，不授权任何新挑战或补偿。后续普通挑战仍需单独确认。\n"
            : "\n只对上述实例和次数有效。目标、账号、连接、程序或校准变化即失效；未确认结果不会重发。\n");
        text.Append("等待确认最长 120 秒。账号、连接、程序或校准变化即失效；停止任务会结束本次运行。");
        return new(kind, external ? 1 : actions.Count, text.ToString(), observed.AddSeconds(120));
    }
    internal static async Task<JObject> AuthorizeAsync(JObject request, JObject config, PcrClanCodeBinding code,
        Func<bool> sourceCurrent, Func<Preview, CancellationToken, Task<bool>> confirm, CancellationToken cancellation)
    {
        var frozen = (JObject)request.DeepClone(); var reply = (JObject)frozen.DeepClone(); reply["ok"] = false;
        try
        {
            cancellation.ThrowIfCancellationRequested();
            if (!sourceCurrent()) return reply;
            var preview = Describe(frozen, config, code, DateTimeOffset.UtcNow);
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellation);
            timeout.CancelAfter(preview.Deadline - DateTimeOffset.UtcNow);
            if (!await confirm(preview, timeout.Token).WaitAsync(timeout.Token).ConfigureAwait(false) || timeout.IsCancellationRequested
                || DateTimeOffset.UtcNow >= preview.Deadline || !sourceCurrent()) return reply;
            if (preview.Kind == "external_regular_baseline")
            {
                if (preview.SelectedSlot is not (>= 1 and <= 3)) return reply;
                reply["selected_slot"] = preview.SelectedSlot.Value;
            }
            reply["ok"] = true;
        }
        catch (Exception ex) when (ex is OperationCanceledException or JsonException or IOException or ArgumentException or InvalidOperationException or OverflowException) { }
        return reply;
    }
    internal static StackPanel Buttons(Preview preview, Action<bool> finish, Action stop)
    {
        var external = preview.Kind == "external_regular_baseline";
        var yes = new Button { Content = preview.Kind == "regular" ? $"仅授权上述 {preview.Count} 次普通挑战" : "仅授权这 1 次补偿", IsDefault = false };
        var no = new Button { Content = external ? "取消登记" : "取消本次消费", IsCancel = true };
        var stopButton = new Button { Content = "停止任务" };
        yes.Click += (_, _) => finish(true); no.Click += (_, _) => finish(false);
        stopButton.Click += (_, _) => { finish(false); stop(); };
        var panel = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12, HorizontalAlignment = HorizontalAlignment.Right };
        panel.Children.Add(stopButton); panel.Children.Add(no);
        if (external)
            for (var slot = 1; slot <= 3; slot++)
            {
                var selected = slot;
                var choice = new Button { Content = $"登记第 {slot} 队已用", IsDefault = false };
                choice.Click += (_, _) => { preview.SelectedSlot = selected; finish(true); };
                panel.Children.Add(choice);
            }
        else panel.Children.Add(yes);
        return panel;
    }
    internal static async Task<bool> ShowAsync(Preview preview, CancellationToken cancellation, Action stop)
    {
        return await Dispatcher.UIThread.InvokeAsync(async () =>
        {
            cancellation.ThrowIfCancellationRequested();
            if (Application.Current?.ApplicationLifetime is not IClassicDesktopStyleApplicationLifetime desktop || desktop.MainWindow == null)
                throw new InvalidDataException("clan_consent_GUI_window_missing");
            var window = new SukiWindow { Title = preview.Kind == "external_regular_baseline" ? "确认程序外已用队伍"
                    : preview.Kind == "regular" ? "本次普通挑战确认" : "本次补偿消费确认",
                Width = 820, Height = 640, MinWidth = 640, MinHeight = 420, WindowStartupLocation = WindowStartupLocation.CenterOwner };
            var body = new TextBlock { Text = preview.Text, TextWrapping = TextWrapping.Wrap };
            var buttons = Buttons(preview, value => window.Close(value && !cancellation.IsCancellationRequested), stop);
            var panel = new DockPanel { Margin = new Thickness(20), LastChildFill = true };
            DockPanel.SetDock(buttons, Dock.Bottom); panel.Children.Add(buttons);
            panel.Children.Add(new ScrollViewer { Content = body, Margin = new Thickness(0, 0, 0, 16) }); window.Content = panel;
            using var registration = cancellation.Register(() => Dispatcher.UIThread.Post(() => { if (window.IsVisible) window.Close(false); }));
            return await window.ShowDialog<bool>(desktop.MainWindow);
        });
    }
}

public static partial class PcrAlphaOwner
{
    private static async Task<JObject> ConfirmClanConsumptionAsync(Session session, JObject request)
    {
        var denied = (JObject)request.DeepClone(); denied["ok"] = false;
        lock (Gate)
        {
            if (session != current || session.ClanCode == null || !session.Consumed || session.Revoked
                || !session.ClanRequests.Add((string?)request["request_id"] ?? "")) { session.Revoked = true; return denied; }
        }
        var config = (JObject)JObject.Parse(session.Config.Json)["config"]!;
        bool SourceCurrent()
        {
            try
            {
                session.Cancel.Token.ThrowIfCancellationRequested(); RequireConfigFile(session);
                if (session.Target != null) RequireSelectedTarget(session.Processor.ViewModel!, session.Target, session.Config);
                if (PcrClanCodeBinding.Capture(repository, config) != session.ClanCode) throw new InvalidDataException("clan_consent_code_changed");
                lock (Gate)
                    if (session == current && session.Consumed && !session.Revoked && session.Watch.Elapsed.TotalSeconds < 1800
                        && (session.Mapping == null || (session.Watch.Elapsed - session.MappingChecked).TotalSeconds <= 15)
                        && (long?)request["root_task_id"] == session.Root && (string?)request["controller_uuid"] == session.Uuid
                        && (string?)request["invocation_id"] == session.Invocation && BindingUnchanged(session)) return true;
            }
            catch (Exception ex) when (ex is IOException or InvalidDataException or UnauthorizedAccessException or InvalidOperationException or OperationCanceledException) { }
            lock (Gate) session.Revoked = true;
            session.Cancel.Cancel(); return false;
        }
        return await PcrClanConsumptionConsent.AuthorizeAsync(request, config, session.ClanCode, SourceCurrent,
            (preview, token) => PcrClanConsumptionConsent.ShowAsync(preview, token, () =>
            {
                Revoke(session.Processor);
                session.Processor.ViewModel?.StopTask();
            }), session.Cancel.Token).ConfigureAwait(false);
    }
}
