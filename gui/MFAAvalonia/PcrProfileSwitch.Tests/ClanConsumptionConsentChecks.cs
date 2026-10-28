using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using MFAAvalonia.Pcr;
using Newtonsoft.Json.Linq;

internal static class ClanConsumptionConsentChecks
{
    private static Func<T, CancellationToken, Task<bool>> Confirm<T>(Func<object, CancellationToken, Task<bool>> callback) =>
        (preview, token) => callback(preview!, token);
    public static void Run(string sourceRoot, string directory)
    {
        var assembly = typeof(PcrAlphaOwner).Assembly;
        var codeType = assembly.GetType("MFAAvalonia.Pcr.PcrClanCodeBinding")!;
        var consentType = assembly.GetType("MFAAvalonia.Pcr.PcrClanConsumptionConsent")!;
        var previewType = consentType.GetNestedType("Preview", BindingFlags.NonPublic)!;
        var flags = BindingFlags.Static | BindingFlags.NonPublic;
        var count = 0;
        void Check(bool value, string name) { if (!value) throw new InvalidOperationException("clan_consumption_consent_" + name); count++; }
        object Invoke(Type type, string name, params object[] arguments) => type.GetMethod(name, flags)!.Invoke(null, arguments)!;
        void Reject(Action action, string name)
        {
            try { action(); } catch (Exception ex) when ((ex is TargetInvocationException { InnerException: { } inner } ? inner : ex) is InvalidDataException or Newtonsoft.Json.JsonException or ArgumentException) { count++; return; }
            throw new InvalidOperationException("clan_consumption_consent_expected_rejection_" + name);
        }
        var fixture = JObject.Parse(File.ReadAllText(Path.Combine(sourceRoot, "tests/data/clan-owner-binding-fixture.json"), Encoding.UTF8));
        var fixtureRoot = Path.Combine(directory, "clan-consent-source", Guid.NewGuid().ToString("N")); Directory.CreateDirectory(fixtureRoot);
        foreach (var row in (JArray)fixture["files"]!)
        {
            var path = Path.Combine(fixtureRoot, (string)row["path"]!); Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.WriteAllText(path, (string)row["text"]!, new UTF8Encoding(false));
        }
        var sourceHash = (string)Invoke(codeType, "SourceHash", fixtureRoot);
        Check(sourceHash == (string?)fixture["sha256"], "cross_language_UTF8_paths_LF_and_CRLF_source_hash");
        var snapshotHash = sourceHash;
        File.AppendAllText(Path.Combine(fixtureRoot, "src/pcr_maa/a.py"), "# changed\n", new UTF8Encoding(false));
        Check((string)Invoke(codeType, "SourceHash", fixtureRoot) != snapshotHash, "source_change_invalidates_binding");
        Reject(() => Invoke(codeType, "Local", fixtureRoot, "../outside.py"), "source_path_escape");
        var coreHash = (string)Invoke(codeType, "CoreHash"); Check(coreHash.Length == 64, "loaded_Core_has_independent_digest");
        var code = Activator.CreateInstance(codeType, new object[] { new string('a', 64), new string('b', 64), new string('c', 64) })!;
        var plans = new JObject();
        for (var slot = 1; slot <= 3; slot++) plans[slot.ToString()] = new JObject
        {
            ["server"] = "cn-bilibili", ["account_id"] = "999000001", ["period"] = "2026-10", ["phase"] = "C", ["boss"] = slot,
            ["round_min"] = 1, ["round_max"] = JValue.CreateNull(), ["support_mode"] = "none", ["content_version"] = "202610141448",
            ["unit_ids"] = new JArray(Enumerable.Range(1, 5).Select(uid => uid + 10 * slot)), ["confirmed_for"] = new string((char)('0' + slot), 64)
        };
        var actualCalibrationHash = (string)Invoke(codeType, "CalibrationHash", sourceRoot, plans);
        Check(actualCalibrationHash.Length == 64, "actual_calibration_files_verified_before_hash");
        Console.WriteLine("clan_consent_source_sha256=" + (string)Invoke(codeType, "SourceHash", sourceRoot));
        Console.WriteLine("clan_consent_calibration_sha256=" + actualCalibrationHash);
        var dailyHash = new string('d', 64);
        var config = new JObject { ["clan_battle_daily"] = new JObject { ["confirmed_for"] = dailyHash, ["slot_plans"] = plans } };
        JObject Request(bool carry = false)
        {
            var now = DateTimeOffset.UtcNow.AddSeconds(-1);
            var day = now.ToOffset(TimeSpan.FromHours(8)).AddHours(-5).ToString("yyyy-MM-dd");
            var binding = new JObject { ["server"] = "cn-bilibili", ["account_id"] = "999000001", ["period"] = "2026-10", ["game_day"] = day,
                ["daily_plan_hash"] = dailyHash, ["slot_confirmations"] = new JObject(plans.Properties().Select(p => new JProperty(p.Name, p.Value["confirmed_for"]!.DeepClone()))) };
            var actions = new JArray();
            for (var slot = 1; slot <= (carry ? 1 : 3); slot++) actions.Add(new JObject
            {
                ["kind"] = carry ? "carryover" : "regular", ["slot"] = slot, ["stock_index"] = carry ? new JValue(0) : JValue.CreateNull(),
                ["boss"] = slot, ["round"] = 16, ["phase"] = "C", ["instance_id"] = "instance-" + slot, ["start_seconds"] = carry ? 35 : 90,
                ["source_intent_id"] = carry ? new JValue(new string('e', 64)) : JValue.CreateNull(),
                ["source_instance_id"] = carry ? new JValue("previous-instance") : JValue.CreateNull(),
                ["using_unit"] = plans[slot.ToString()]!["unit_ids"]!.DeepClone(), ["support_unit"] = JValue.CreateNull(), ["support_owner_viewer_id"] = JValue.CreateNull()
            });
            return new JObject { ["kind"] = "clan_consumption", ["request_id"] = Guid.NewGuid().ToString("N"), ["root_task_id"] = 7,
                ["controller_uuid"] = "synthetic-controller", ["invocation_id"] = Guid.NewGuid().ToString("N"), ["daily_plan_hash"] = dailyHash, ["timeout_seconds"] = 120,
                ["preview"] = new JObject { ["server"] = "cn-bilibili", ["account_id"] = "999000001", ["period"] = "2026-10", ["game_day"] = day,
                    ["batch_id"] = PcrClanGuide.Fingerprint(binding), ["source_sha256"] = new string('a', 64), ["calibration_sha256"] = new string('b', 64),
                    ["owner_core_sha256"] = new string('c', 64), ["kind"] = carry ? "carryover" : "regular", ["max_count"] = actions.Count,
                    ["observed_at"] = now.ToString("O"), ["remaining_count"] = 3, ["cp"] = 900, ["actions"] = actions } };
        }
        JObject ExternalRequest()
        {
            var request = Request(); var preview = (JObject)request["preview"]!;
            preview["kind"] = "external_regular_baseline"; preview["max_count"] = 1; preview["remaining_count"] = 2;
            return request;
        }
        object Describe(JObject request) => Invoke(consentType, "Describe", request, config, code, DateTimeOffset.UtcNow);
        string Description(object preview) => (string)previewType.GetProperty("Text")!.GetValue(preview)!;
        var regular = Request(); var regularDescription = Description(Describe(regular));
        Check(regularDescription.Contains("3 次普通挑战") && regularDescription.Contains("instance-1") && regularDescription.Contains("instance-3")
            && regularDescription.Contains("当前普通剩余 3 次") && regularDescription.Contains("补偿另行确认"), "regular_preview_lists_exact_instances_and_budget");
        var ownJob = (JObject)plans["1"]!; var originalJob = (JObject)ownJob.DeepClone();
        ownJob["support_mode"] = "qsjs";
        var ownSnapshot = new JObject
        {
            ["schema_version"] = 1, ["server"] = ownJob["server"]!.DeepClone(), ["account_id"] = ownJob["account_id"]!.DeepClone(),
            ["content_version"] = ownJob["content_version"]!.DeepClone(), ["observed_at"] = DateTimeOffset.UtcNow.AddSeconds(-1).ToString("O"),
            ["ownership_complete"] = true, ["unknown_fields"] = new JArray(),
            ["units"] = new JArray(ownJob["unit_ids"]!.Values<int>().Select(uid => new JObject { ["unit_id"] = uid, ["battle_rarity"] = 4 }))
        };
        void BindOwnSnapshot(JObject value)
        {
            var path = Path.Combine(fixtureRoot, "synthetic-own-account.json"); File.WriteAllText(path, value.ToString(), new UTF8Encoding(false));
            ownJob["snapshot"] = new JObject { ["path"] = path, ["sha256"] = PcrClanGuide.Stamp(path) };
        }
        BindOwnSnapshot(ownSnapshot); var ownedMaterial = ownJob.ToString();
        Check(Description(Describe(Request())).Contains("3 次普通挑战") && ownJob.ToString() == ownedMaterial, "owned_qsjs_uses_exact_same_consent_without_rewriting_plan");
        var ownedPath = (string)ownJob["snapshot"]!["path"]!; File.AppendAllText(ownedPath, "\n");
        Reject(() => Describe(Request()), "owned_qsjs_source_changed");
        var wrongOwnedAccount = (JObject)ownSnapshot.DeepClone(); wrongOwnedAccount["account_id"] = "999000002"; BindOwnSnapshot(wrongOwnedAccount);
        Reject(() => Describe(Request()), "owned_qsjs_wrong_account");
        var staleOwned = (JObject)ownSnapshot.DeepClone(); staleOwned["observed_at"] = DateTimeOffset.UtcNow.AddHours(-25).ToString("O"); BindOwnSnapshot(staleOwned);
        Reject(() => Describe(Request()), "owned_qsjs_expired_account");
        var unknownOwned = (JObject)ownSnapshot.DeepClone(); unknownOwned["unknown_fields"] = new JArray("units.11.ownership"); BindOwnSnapshot(unknownOwned);
        Reject(() => Describe(Request()), "owned_qsjs_unknown_ownership");
        var absentOwned = (JObject)ownSnapshot.DeepClone(); ((JArray)absentOwned["units"]!).RemoveAt(0); BindOwnSnapshot(absentOwned);
        Reject(() => Describe(Request()), "qsjs_actual_borrowed_not_authorized");
        BindOwnSnapshot(ownSnapshot); ownJob["support_mode"] = "specified"; ownJob["support_unit_id"] = 11;
        Reject(() => Describe(Request()), "specified_owned_still_not_authorized");
        plans["1"] = originalJob;
        var stoppedFromDialog = 0; bool? clickedConsent = null;
        var buttons = (Avalonia.Controls.StackPanel)Invoke(consentType, "Buttons", Describe(regular),
            new Action<bool>(value => clickedConsent = value), new Action(() => stoppedFromDialog++));
        var stopButton = buttons.Children.OfType<Avalonia.Controls.Button>().Single(button => (string?)button.Content == "停止任务");
        stopButton.RaiseEvent(new Avalonia.Interactivity.RoutedEventArgs(Avalonia.Controls.Button.ClickEvent));
        Check(stoppedFromDialog == 1 && clickedConsent == false, "modal_dialog_keeps_explicit_Stop_without_authorizing");
        Check(buttons.Children.OfType<Avalonia.Controls.Button>().All(button => !button.IsDefault), "no_default_keypress_authorizes_consumption");
        var external = ExternalRequest(); var externalPreview = Describe(external); var externalDescription = Description(externalPreview);
        Check(externalDescription.Contains("仅登记今天程序外已用队伍") && externalDescription.Contains("不授权消费")
            && externalDescription.Contains("不记为程序结算") && externalDescription.Contains("后续普通挑战仍需单独确认")
            && (int)previewType.GetProperty("Count")!.GetValue(externalPreview)! == 1,
            "external_baseline_is_fact_only_not_consumption_or_settlement");
        var externalButtons = (Avalonia.Controls.StackPanel)Invoke(consentType, "Buttons", externalPreview,
            new Action<bool>(value => clickedConsent = value), new Action(() => stoppedFromDialog++));
        Check(externalButtons.Children.OfType<Avalonia.Controls.Button>().Count() == 5
            && externalButtons.Children.OfType<Avalonia.Controls.Button>().All(button => !button.IsDefault), "external_choices_have_no_default_authorization");
        var thirdChoice = externalButtons.Children.OfType<Avalonia.Controls.Button>().Single(button => (string?)button.Content == "登记第 3 队已用");
        clickedConsent = null; thirdChoice.RaiseEvent(new Avalonia.Interactivity.RoutedEventArgs(Avalonia.Controls.Button.ClickEvent));
        var selectedProperty = previewType.GetProperty("SelectedSlot", BindingFlags.Instance | BindingFlags.NonPublic)!;
        Check(clickedConsent == true && (int?)selectedProperty.GetValue(externalPreview) == 3, "external_fact_button_records_exact_selected_slot");
        foreach (var mutation in new (string Name, Action<JObject> Change)[]
        {
            ("wrong_team", value => value["preview"]!["actions"]![0]!["using_unit"]![0] = 999),
            ("changed_source", value => value["preview"]!["source_sha256"] = new string('f', 64)),
            ("changed_calibration", value => value["preview"]!["calibration_sha256"] = new string('f', 64)),
            ("wrong_day", value => value["preview"]!["game_day"] = "2000-01-01"),
            ("wrong_account", value => value["preview"]!["account_id"] = "999000002"),
            ("wrong_slot", value => value["preview"]!["actions"]![2]!["slot"] = 4),
            ("duplicate_slot", value => value["preview"]!["actions"]![2]!["slot"] = 1),
            ("wrong_budget", value => value["preview"]!["max_count"] = 3),
            ("missing_candidate", value => ((JArray)value["preview"]!["actions"]!).RemoveAt(2)),
            ("wrong_CP", value => value["preview"]!["cp"] = 600),
            ("second_external_gap", value => value["preview"]!["remaining_count"] = 1),
            ("carry_not_regular_fact", value => value["preview"]!["actions"]![0]!["kind"] = "carryover")
        })
        {
            var request = ExternalRequest(); mutation.Change(request); Reject(() => Describe(request), "external_" + mutation.Name);
        }
        var carryover = Request(true); var carryDescription = Description(Describe(carryover));
        Check(carryDescription.Contains("1 次补偿") && carryDescription.Contains("库存 0") && carryDescription.Contains("35 秒")
            && carryDescription.Contains("previous-instance") && carryDescription.Contains(new string('e', 64)) && carryDescription.Contains("队伍"), "carryover_is_independent_stock_source_team_and_time");
        foreach (var mutation in new (string Name, Action<JObject> Change)[]
        {
            ("unknown_field", value => value["authority"] = true),
            ("uppercase_nonce_alias", value => value["request_id"] = new string('A', 32)),
            ("integer_overflow", value => value["preview"]!["cp"] = JToken.Parse("999999999999999999999999999")),
            ("wrong_daily_plan", value => value["daily_plan_hash"] = new string('f', 64)),
            ("changed_source", value => value["preview"]!["source_sha256"] = new string('f', 64)),
            ("changed_calibration", value => value["preview"]!["calibration_sha256"] = new string('f', 64)),
            ("changed_Core", value => value["preview"]!["owner_core_sha256"] = new string('f', 64)),
            ("wrong_account", value => value["preview"]!["account_id"] = "999000002"),
            ("wrong_day", value => value["preview"]!["game_day"] = "2000-01-01"),
            ("stale_observation", value => value["preview"]!["observed_at"] = DateTimeOffset.UtcNow.AddSeconds(-61).ToString("O")),
            ("future_observation", value => value["preview"]!["observed_at"] = DateTimeOffset.UtcNow.AddMinutes(1).ToString("O")),
            ("missing_timezone", value => value["preview"]!["observed_at"] = "2026-10-26T12:00:00"),
            ("wrong_budget", value => value["preview"]!["max_count"] = 2),
            ("insufficient_count", value => value["preview"]!["remaining_count"] = 2),
            ("regular_cannot_authorize_carry", value => value["preview"]!["actions"]![0]!["kind"] = "carryover"),
            ("wrong_team", value => value["preview"]!["actions"]![0]!["using_unit"]![0] = 999),
            ("wrong_boss", value => value["preview"]!["actions"]![0]!["boss"] = 5),
            ("instance_control_char", value => value["preview"]!["actions"]![0]!["instance_id"] = "wrong\ninstance"),
            ("regular_has_no_stock", value => value["preview"]!["actions"]![0]!["stock_index"] = 0)
        })
        {
            var request = Request(); mutation.Change(request); Reject(() => Describe(request), mutation.Name);
        }
        var wrongSource = Request(true); wrongSource["preview"]!["actions"]![0]!["source_instance_id"] = "instance-1";
        Reject(() => Describe(wrongSource), "carryover_requires_new_instance");
        Reject(() => Invoke(consentType, "Parse", "{\"kind\":1,\"kind\":2}"), "duplicate_wire_keys");
        Reject(() => Invoke(consentType, "Parse", "{} {}"), "trailing_wire_document");
        Task<JObject> Authorize(JObject request, Func<bool> sourceCurrent, Func<object, CancellationToken, Task<bool>> callback, CancellationToken cancel = default)
        {
            var adapter = typeof(ClanConsumptionConsentChecks).GetMethod(nameof(Confirm), flags)!.MakeGenericMethod(previewType).Invoke(null, new object[] { callback })!;
            return (Task<JObject>)Invoke(consentType, "AuthorizeAsync", request, config, code, sourceCurrent, adapter, cancel);
        }
        var approved = Authorize(regular, () => true, (_, _) => Task.FromResult(true)).GetAwaiter().GetResult();
        var expected = (JObject)regular.DeepClone(); expected["ok"] = true;
        Check(JToken.DeepEquals(approved, expected) && !regular.ContainsKey("ok"), "approval_is_exact_echo_and_does_not_mutate_preview");
        Check((bool?)Authorize(Request(), () => true, (_, _) => Task.FromResult(false)).GetAwaiter().GetResult()["ok"] == false, "cancel_does_not_authorize");
        var externalApproved = Authorize(external, () => true, (preview, _) =>
        {
            selectedProperty.SetValue(preview, 3); return Task.FromResult(true);
        }).GetAwaiter().GetResult();
        var externalExpected = (JObject)external.DeepClone(); externalExpected["selected_slot"] = 3; externalExpected["ok"] = true;
        Check(JToken.DeepEquals(externalApproved, externalExpected) && !external.ContainsKey("selected_slot"), "external_reply_echoes_fact_selection_only");
        Check((bool?)Authorize(ExternalRequest(), () => true, (_, _) => Task.FromResult(true)).GetAwaiter().GetResult()["ok"] == false,
            "external_true_without_slot_is_rejected");
        foreach (var invalidSlot in new[] { 0, 4 })
            Check((bool?)Authorize(ExternalRequest(), () => true, (preview, _) =>
            {
                selectedProperty.SetValue(preview, invalidSlot); return Task.FromResult(true);
            }).GetAwaiter().GetResult()["ok"] == false, "external_invalid_selected_slot_" + invalidSlot);
        var externalDenied = Authorize(ExternalRequest(), () => true, (preview, _) =>
        {
            selectedProperty.SetValue(preview, 3); return Task.FromResult(false);
        }).GetAwaiter().GetResult();
        Check((bool?)externalDenied["ok"] == false && !externalDenied.ContainsKey("selected_slot"), "external_cancel_never_records_selection");
        var prompts = 0;
        Check((bool?)Authorize(Request(), () => false, (_, _) => { prompts++; return Task.FromResult(true); }).GetAwaiter().GetResult()["ok"] == false && prompts == 0,
            "source_invalid_before_prompt");
        var sourceChecks = 0;
        Check((bool?)Authorize(Request(), () => ++sourceChecks == 1, (_, _) => Task.FromResult(true)).GetAwaiter().GetResult()["ok"] == false,
            "source_changed_while_awaiting_confirmation");
        using var stopped = new CancellationTokenSource();
        var waiting = Authorize(Request(), () => true, async (_, token) => { await Task.Delay(Timeout.Infinite, token); return true; }, stopped.Token);
        stopped.Cancel();
        Check((bool?)waiting.GetAwaiter().GetResult()["ok"] == false, "Stop_cancels_wait_without_grant");
        Console.WriteLine($"clan_consumption_consent_checks={count}; synthetic previews only, game input=0");
    }
}
