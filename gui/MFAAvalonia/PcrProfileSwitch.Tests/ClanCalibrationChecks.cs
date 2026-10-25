using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using MFAAvalonia.Pcr;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

internal static class ClanCalibrationChecks
{
    public static void Run(string sourceRoot, string directory)
    {
        var root = Path.GetFullPath(Path.Combine(directory, "clan-calibration", Guid.NewGuid().ToString("N")));
        var indexPath = Path.Combine(root, PcrClanCalibration.IndexPath);
        var profilePath = Path.Combine(root, "assets/resource/clan-battle-calibrations/synthetic.json");
        Directory.CreateDirectory(Path.GetDirectoryName(indexPath)!);
        const string contentVersion = "202610250001";
        var count = 0;
        void Check(bool value, string name) { if (!value) throw new InvalidOperationException("clan_calibration_" + name); count++; }
        void Reject(Action action, string name, string code)
        {
            try { action(); }
            catch (InvalidOperationException ex) when (ex.Message.Contains(code)) { count++; return; }
            throw new InvalidOperationException("clan_calibration_expected_rejection_" + name);
        }
        var reader = typeof(PcrClanCalibration).GetMethod("Read", BindingFlags.Static | BindingFlags.NonPublic,
            null, new[] { typeof(string), typeof(string), typeof(string) }, null)!;
        PcrClanCalibration Read(string? version = contentVersion, string? digest = null) =>
            (PcrClanCalibration)reader.Invoke(null, new object?[] { root, version, digest ?? PcrClanGuide.Hash(File.ReadAllBytes(indexPath)) })!;
        JObject Dependency(string relative, string content)
        {
            var path = Path.Combine(root, relative); Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.WriteAllText(path, content, new UTF8Encoding(false));
            return new JObject { ["path"] = relative, ["sha256"] = PcrClanGuide.Hash(File.ReadAllBytes(path)) };
        }
        JObject Sample(string name)
        {
            var value = Dependency("assets/resource/clan-battle-calibrations/" + name + ".sample", "synthetic " + name);
            value["page"] = name; return value;
        }
        JObject Profile(int version = 2)
        {
            var template = Dependency("assets/resource/image/clan-synthetic/first.png", "synthetic template one");
            _ = Dependency("assets/resource/image/clan-synthetic/second.png", "synthetic template two");
            var profile = new JObject
            {
                ["version"] = version, ["calibration_id"] = "synthetic-capabilities", ["server"] = "cn-bilibili",
                ["frame_size"] = new JArray(1280, 720), ["evidence_kind"] = "device_saved_frame",
                ["battle_hud"] = "guide-trial-1280x720", ["actions"] = new JObject(), ["formation"] = new JObject(), ["support"] = new JObject(),
                ["pages"] = new JObject { ["main"] = new JObject { ["recognition"] = "TemplateMatch", ["template"] = "clan-synthetic/first.png", ["sha256"] = template["sha256"]!.DeepClone() } },
                ["samples"] = new JArray(Sample("main")),
                ["daily"] = new JObject
                {
                    ["version"] = 2, ["samples"] = new JArray(Sample("daily")),
                    ["execution"] = new JObject
                    {
                        ["pages"] = new JObject(), ["actions"] = new JObject(), ["used_units"] = new JObject(), ["carry_slots"] = new JObject(),
                        ["boss_slots"] = new JObject(), ["formation_owner"] = new JObject(), ["formation_mode"] = new JObject(), ["speed_one"] = new JObject(),
                        ["samples"] = new JArray(Sample("execution"))
                    }
                }
            };
            var measured = new JObject
            {
                ["kind"] = "owner_native_clan_timing", ["calibration_id"] = profile["calibration_id"]!.DeepClone(),
                ["battle_hud"] = profile["battle_hud"]!.DeepClone(), ["observation_scope"] = "full_read_tick",
                ["action_scope"] = "toggle_dispatch_and_full_team_readback", ["battle_speed"] = 1,
                ["observation_intervals_ms"] = new JArray(Enumerable.Repeat(1, 20)), ["action_latencies_ms"] = new JArray(Enumerable.Repeat(1, 20)),
                ["game_second_ms"] = new JArray(Enumerable.Repeat(1000, 20))
            };
            profile["timing_measurements"] = Dependency("assets/resource/clan-battle-calibrations/timing.json", measured.ToString(Formatting.None));
            return profile;
        }
        JObject Index(int version, JObject profile, params string[] capabilities)
        {
            File.WriteAllText(profilePath, profile.ToString(Formatting.None), new UTF8Encoding(false));
            var entry = new JObject
            {
                ["calibration_id"] = "synthetic-capabilities", ["server"] = "cn-bilibili", ["content_version"] = contentVersion,
                ["frame_size"] = new JArray(1280, 720), ["profile_path"] = "assets/resource/clan-battle-calibrations/synthetic.json",
                ["profile_sha256"] = PcrClanGuide.Hash(File.ReadAllBytes(profilePath))
            };
            if (version == 2) entry["capabilities"] = new JArray(capabilities);
            return new JObject { ["version"] = version, ["entries"] = new JArray(entry) };
        }
        PcrClanCalibration Load(JObject index)
        {
            File.WriteAllText(indexPath, index.ToString(Formatting.None), new UTF8Encoding(false));
            return Read();
        }
        JObject Entry(JObject index) => (JObject)index["entries"]![0]!;
        JObject Plan(bool auto = true, bool timed = false) => new()
        {
            ["content_version"] = contentVersion, ["auto"] = auto, ["support_mode"] = "none",
            ["switches"] = timed ? new JArray(new JObject { ["remaining_seconds"] = 60, ["auto"] = false, ["set_by_unit"] = new JObject() }) : new JArray()
        };
        void Unavailable(PcrClanCalibration value, string name) => Check(value.Error != null && !value.Capabilities.Any()
            && !value.Supports("simulation") && !value.Supports("formal"), name);

        var bundledIndex = Path.Combine(sourceRoot, PcrClanCalibration.IndexPath);
        Check(PcrClanGuide.Hash(File.ReadAllBytes(bundledIndex)) == PcrClanCalibration.IndexSha256, "gui_pin_matches_bundled_index");
        var python = File.ReadAllText(Path.Combine(sourceRoot, "src/pcr_maa/clan_battle_calibration.py"), Encoding.UTF8);
        Check(Regex.Match(python, "INDEX_SHA256 = '([0-9a-f]{64})'").Groups[1].Value == PcrClanCalibration.IndexSha256,
            "python_and_gui_share_index_pin");
        Unavailable(PcrClanCalibration.Read(root, contentVersion), "missing_index_is_unavailable");
        var index = Index(2, Profile(), "simulation");
        var limited = Load(index);
        Check(limited.Supports("simulation") && !limited.Supports("timed") && !limited.Supports("formal") && !limited.Supports("carryover"),
            "v2_only_grants_declared_capabilities");
        var trailingRoot = (PcrClanCalibration)reader.Invoke(null, new object?[]
            { root + Path.DirectorySeparatorChar, contentVersion, PcrClanGuide.Hash(File.ReadAllBytes(indexPath)) })!;
        Check(trailingRoot.Supports("simulation"), "root_trailing_separator_is_supported");
        limited.Require(Plan(false), "simulation"); count++;
        Reject(() => limited.Require(Plan(), "formal"), "formal_missing", "capability_missing:formal");
        Reject(() => limited.Require(Plan(timed: true), "simulation"), "timed_missing", "capability_missing:timed");
        Unavailable(PcrClanCalibration.Read(root, contentVersion), "public_reader_does_not_trust_synthetic_index");
        Unavailable(Read(digest: new string('0', 64)), "wrong_index_hash");
        Unavailable(Read("202610250002"), "missing_content_version");
        Unavailable(Read(null), "missing_account_content_version");

        var full = Load(Index(2, Profile(), "simulation", "timed", "formal", "carryover"));
        foreach (var capability in new[] { "simulation", "formal", "carryover" }) { full.Require(Plan(timed: true), capability); count++; }
        Check(full.Summary.Contains("模拟战：已校准") && full.Summary.Contains("秒数轴：测量资料齐全，开始前核对时限")
            && full.Summary.Contains("正式三刀：已校准") && full.Summary.Contains("补偿刀：已校准"), "summary_describes_all_capabilities");
        var empty = Load(Index(2, Profile()));
        Check(empty.Error == null && !empty.Capabilities.Any(), "empty_capabilities_mean_not_open");
        Reject(() => empty.Require(Plan(), "simulation"), "empty_capabilities_stop_simulation", "capability_missing:simulation");
        var ownProfile = Profile(); ownProfile["support_modes"] = new JArray("none");
        var ownOnly = Load(Index(2, ownProfile, "simulation", "timed", "formal", "carryover"));
        Check(ownOnly.OwnUnitsOnly && ownOnly.Summary.Contains("仅全自有队伍可用；借人身份尚未校准"), "own_only_scope_is_visible");
        ownOnly.Require(Plan(), "simulation"); count++;
        foreach (var support in new[] { "qsjs", "specified", "unknown" })
        {
            var borrowed = Plan(); borrowed["support_mode"] = support;
            Reject(() => ownOnly.Require(borrowed, "simulation"), "uncalibrated_support_" + support, "clan_battle_support_mode_uncalibrated");
            Reject(() => ownOnly.Require(borrowed, "formal"), "uncalibrated_formal_support_" + support, "clan_battle_support_mode_uncalibrated");
        }
        var missingSupport = Plan(); missingSupport.Remove("support_mode");
        Reject(() => ownOnly.Require(missingSupport, "simulation"), "missing_support_scope_stops", "clan_battle_support_mode_uncalibrated");
        foreach (var invalid in new JToken[] { new JArray(), new JArray("none", "none"), new JArray("qsjs"), new JArray("specified"), new JValue("none"), JValue.CreateNull() })
        {
            var profile = Profile(); profile["support_modes"] = invalid;
            Unavailable(Load(Index(2, profile, "simulation")), "invalid_support_scope_" + invalid.ToString(Formatting.None));
        }
        var legacySupport = Profile(1); legacySupport["support_modes"] = new JArray("none");
        Unavailable(Load(Index(1, legacySupport)), "v1_cannot_declare_support_scope");
        Check(!full.OwnUnitsOnly, "absent_support_scope_keeps_legacy_behavior");
        var ownWithoutSupport = Profile(); ownWithoutSupport["support_modes"] = new JArray("none"); ownWithoutSupport.Remove("support");
        Check(Load(Index(2, ownWithoutSupport, "simulation")).Supports("simulation"), "own_only_needs_no_borrowed_support_calibration");

        var accountProfile = Profile(); accountProfile["support_modes"] = new JArray("none");
        accountProfile["formation"]!["star_source"] = "account_snapshot";
        var accountCalibration = Load(Index(2, accountProfile, "simulation"));
        Check(accountCalibration.AccountStars && accountCalibration.OwnUnitsOnly, "account_star_scope");
        var accountData = new JObject
        {
            ["schema_version"] = 1, ["server"] = "cn-bilibili", ["account_id"] = "999000001", ["content_version"] = contentVersion,
            ["observed_at"] = DateTimeOffset.UtcNow.AddSeconds(-1).ToString("O"), ["unknown_fields"] = new JArray(),
            ["units"] = new JArray(Enumerable.Range(1, 5).Select(id => new JObject { ["unit_id"] = id, ["rarity"] = 6, ["battle_rarity"] = 5 }))
        };
        JObject AccountPlan(JObject data)
        {
            var file = Path.Combine(root, "synthetic-account.json");
            File.WriteAllText(file, data.ToString(Formatting.None), new UTF8Encoding(false));
            var value = Plan(); value["server"] = "cn-bilibili"; value["account_id"] = "999000001";
            value["unit_ids"] = new JArray(Enumerable.Range(1, 5)); value["stars"] = new JArray(Enumerable.Repeat(5, 5));
            value["snapshot"] = new JObject { ["path"] = file, ["sha256"] = PcrClanGuide.Hash(File.ReadAllBytes(file)) };
            return value;
        }
        accountCalibration.Require(AccountPlan(accountData), "simulation"); count++;
        var validAccount = (JObject)accountData.DeepClone(); validAccount["observed_at"] = DateTimeOffset.UtcNow.AddHours(-23).ToString("O");
        accountCalibration.Require(AccountPlan(validAccount), "simulation"); count++;
        var staleAccount = (JObject)accountData.DeepClone(); staleAccount["observed_at"] = DateTimeOffset.UtcNow.AddHours(-25).ToString("O");
        Reject(() => accountCalibration.Require(AccountPlan(staleAccount), "simulation"), "account_stars_expired", "clan_battle_snapshot_expired");
        var wrongStar = (JObject)accountData.DeepClone(); wrongStar["units"]![0]!["battle_rarity"] = 3;
        Reject(() => accountCalibration.Require(AccountPlan(wrongStar), "simulation"), "account_stars_mismatch", "clan_battle_account_stars_mismatch");
        var unknownStar = (JObject)accountData.DeepClone(); unknownStar["unknown_fields"] = new JArray("units.1.battle_rarity");
        Reject(() => accountCalibration.Require(AccountPlan(unknownStar), "simulation"), "account_stars_unknown", "clan_battle_account_stars_unknown");
        var ambiguousSource = Profile(); ambiguousSource["formation"]!["star_source"] = "account_snapshot";
        Unavailable(Load(Index(2, ambiguousSource, "simulation")), "account_star_source_requires_own_scope");

        foreach (var key in new[] { "battle_hud", "pages", "actions", "formation", "support" })
        {
            var incomplete = Profile(); incomplete.Remove(key);
            Unavailable(Load(Index(2, incomplete, "simulation")), "missing_required_structure_" + key);
        }
        foreach (var section in new[] { "root", "daily", "execution" })
        {
            var profile = Profile();
            var samples = (JArray)(section == "root" ? profile["samples"]! : section == "daily" ? profile["daily"]!["samples"]! : profile["daily"]!["execution"]!["samples"]!);
            var samplePath = Path.Combine(root, (string)samples[0]!["path"]!);
            Load(Index(2, profile, "simulation")); File.AppendAllText(samplePath, "changed", new UTF8Encoding(false));
            Unavailable(Read(), "changed_" + section + "_sample");
            File.Delete(samplePath); Unavailable(Read(), "missing_" + section + "_sample");
        }
        var missingSamples = Profile(); missingSamples["samples"] = new JArray();
        Unavailable(Load(Index(2, missingSamples, "simulation")), "empty_sample_list");
        var badSample = Profile(); ((JObject)badSample["samples"]![0]!).Remove("page");
        Unavailable(Load(Index(2, badSample, "simulation")), "sample_reference_shape");

        var arrayProfile = Profile();
        var spec = (JObject)arrayProfile["pages"]!["main"]!;
        spec["template"] = new JArray("clan-synthetic/first.png", "clan-synthetic/second.png");
        spec["sha256"] = new JArray(new[] { "first", "second" }.Select(name => PcrClanGuide.Hash(File.ReadAllBytes(Path.Combine(root, "assets/resource/image/clan-synthetic/" + name + ".png")))));
        Check(Load(Index(2, arrayProfile, "simulation")).Supports("simulation"), "template_array_hashes_are_supported");
        var secondTemplate = Path.Combine(root, "assets/resource/image/clan-synthetic/second.png");
        File.AppendAllText(secondTemplate, "changed", new UTF8Encoding(false)); Unavailable(Read(), "template_array_second_hash_is_verified");
        File.Delete(secondTemplate); Unavailable(Read(), "template_array_missing_dependency");
        foreach (var change in new (string Name, Action<JObject> Change)[]
        {
            ("string_hash_changed", value => value["sha256"] = new string('0', 64)),
            ("array_hash_length", value => { value["template"] = new JArray("clan-synthetic/first.png", "clan-synthetic/second.png"); value["sha256"] = new JArray(value["sha256"]!.DeepClone()); }),
            ("array_with_scalar_hash", value => value["template"] = new JArray("clan-synthetic/first.png")),
            ("template_missing", value => value.Remove("template"))
        })
        {
            var profile = Profile(); change.Change((JObject)profile["pages"]!["main"]!);
            Unavailable(Load(Index(2, profile, "simulation")), change.Name);
        }
        var timingProfile = Profile(); Load(Index(2, timingProfile, "simulation", "timed"));
        var timingPath = Path.Combine(root, (string)timingProfile["timing_measurements"]!["path"]!);
        File.AppendAllText(timingPath, " ", new UTF8Encoding(false)); Unavailable(Read(), "timing_file_changed");
        File.Delete(timingPath); Unavailable(Read(), "timing_file_missing");
        foreach (var change in new (string Name, Action<JObject> Change)[]
        {
            ("kind", value => value["kind"] = "unbound"),
            ("calibration_id", value => value["calibration_id"] = "other"),
            ("battle_hud", value => value["battle_hud"] = new JObject { ["other"] = true }),
            ("observation_scope", value => value.Remove("observation_scope")),
            ("action_scope", value => value["action_scope"] = "dispatch_only"),
            ("battle_speed", value => value["battle_speed"] = 2),
            ("short_measurements", value => value["observation_intervals_ms"] = new JArray(1)),
            ("nonpositive_measurement", value => value["action_latencies_ms"]![0] = 0),
            ("missing_measurements", value => value.Remove("game_second_ms"))
        })
        {
            var profile = Profile(); var binding = (JObject)profile["timing_measurements"]!;
            var measured = JObject.Parse(File.ReadAllText(Path.Combine(root, (string)binding["path"]!), Encoding.UTF8)); change.Change(measured);
            profile["timing_measurements"] = Dependency((string)binding["path"]!, measured.ToString(Formatting.None));
            Unavailable(Load(Index(2, profile, "simulation", "timed")), "timing_fields_" + change.Name);
        }
        var noTiming = Profile(); noTiming.Remove("timing_measurements");
        Unavailable(Load(Index(2, noTiming, "simulation", "timed")), "missing_timing_binding");
        foreach (var change in new (string Name, Action<JObject> Change)[]
        {
            ("daily", value => value.Remove("daily")),
            ("daily_version", value => value["daily"]!["version"] = 1),
            ("execution", value => ((JObject)value["daily"]!).Remove("execution")),
            ("execution_key", value => ((JObject)value["daily"]!["execution"]!).Remove("used_units")),
            ("execution_extra", value => value["daily"]!["execution"]!["extra"] = true)
        })
        {
            var profile = Profile(); change.Change(profile);
            Unavailable(Load(Index(2, profile, "formal", "carryover")), "formal_structure_" + change.Name);
        }

        var legacy = Load(Index(1, Profile(1)));
        Check(legacy.Version == 1 && legacy.Supports("simulation") && !legacy.Supports("formal"), "v1_is_simulation_only");
        Check(legacy.Summary.Contains("仅全程 AUTO、无切换"), "legacy_summary_explains_simulation_limit");
        legacy.Require(Plan(), "simulation"); count++;
        Reject(() => legacy.Require(Plan(false), "simulation"), "legacy_auto_required", "calibration_auto_only");
        Reject(() => legacy.Require(Plan(timed: true), "simulation"), "legacy_no_timing", "calibration_auto_only");
        var legacyIndex = Load(Index(1, Profile(2)));
        Check(legacyIndex.Version == 1, "v1_index_keeps_legacy_limit_with_v2_profile");
        Reject(() => legacyIndex.Require(Plan(false), "simulation"), "v1_index_still_requires_auto", "calibration_auto_only");
        Check(Load(Index(2, Profile(1), "simulation")).Version == 1, "v2_index_preserves_legacy_profile_limit");
        Unavailable(Load(Index(2, Profile(1), "simulation", "timed")), "legacy_profile_cannot_grant_timing");
        Unavailable(Load(Index(2, Profile(1), "formal")), "legacy_profile_cannot_grant_formal");

        foreach (var capabilities in new[] { new[] { "unknown" }, new[] { "simulation", "simulation" }, new[] { "carryover" } })
            Unavailable(Load(Index(2, Profile(), capabilities)), "invalid_capabilities_" + string.Join('_', capabilities));
        foreach (var change in new (string Name, Action<JObject> Change)[]
        {
            ("unknown_index_field", value => value["extra"] = true),
            ("unknown_index_version", value => value["version"] = 3),
            ("overflow_index_version", value => value["version"] = long.MaxValue),
            ("wrong_server", value => Entry(value)["server"] = "other"),
            ("bad_content_version", value => Entry(value)["content_version"] = "2026"),
            ("wrong_frame_size", value => Entry(value)["frame_size"] = new JArray(1920, 1080)),
            ("duplicate_identity", value => ((JArray)value["entries"]!).Add(Entry(value).DeepClone())),
            ("invalid_profile_hash", value => Entry(value)["profile_sha256"] = "not-a-digest"),
            ("escaping_profile_path", value => Entry(value)["profile_path"] = "../outside.json"),
            ("capabilities_not_array", value => Entry(value)["capabilities"] = "simulation")
        })
        {
            index = Index(2, Profile(), "simulation"); change.Change(index);
            Unavailable(Load(index), change.Name);
        }
        var mismatch = Profile(); mismatch["calibration_id"] = "different";
        Unavailable(Load(Index(2, mismatch, "simulation")), "profile_identity_mismatch");
        index = Index(2, Profile(), "simulation"); Load(index);
        File.AppendAllText(profilePath, " ", new UTF8Encoding(false));
        Unavailable(Read(), "profile_bytes_changed");
        File.Delete(profilePath);
        Unavailable(Read(), "profile_missing");
        File.WriteAllText(indexPath, "{\"version\":2,\"version\":2,\"entries\":[]}", new UTF8Encoding(false));
        Unavailable(Read(), "duplicate_json_key");

        Load(Index(2, Profile(), "formal", "carryover"));
        var request = new JObject { ["slot_plans"] = new JObject { ["1"] = Plan(), ["2"] = Plan(), ["3"] = Plan() } };
        var before = request.ToString(Formatting.None);
        Reject(() => PcrClanDailyExecution.RequireCapabilities(request, root), "formal_gate_uses_pinned_reader", "production_daily_adapter_unavailable");
        Check(request.ToString(Formatting.None) == before, "formal_gate_preserves_request");
        foreach (var invalid in new[]
        {
            new JObject(), new JObject { ["slot_plans"] = new JObject() },
            new JObject { ["slot_plans"] = new JObject { ["1"] = Plan(), ["2"] = Plan(), ["4"] = Plan() } },
            new JObject { ["slot_plans"] = new JObject { ["1"] = Plan(), ["2"] = Plan(), ["3"] = true } }
        })
        {
            try { PcrClanDailyExecution.RequireCapabilities(invalid, root); }
            catch (InvalidDataException ex) when (ex.Message.Contains("完整三目标")) { count++; continue; }
            throw new InvalidOperationException("clan_calibration_expected_rejection_incomplete_formal_targets");
        }
        var autoCalibration = new PcrClanCalibration(2, new HashSet<string> { "simulation", "timed", "formal" }, null,
            AutoBattleSpeeds: new HashSet<int> { 1, 2, 4 });
        foreach (var speed in new[] { 1, 2, 4 }) { autoCalibration.Require(Plan(false), "simulation", speed); count++; }
        Reject(() => autoCalibration.Require(Plan(), "simulation", 3), "invalid_speed", "speed_invalid");
        Reject(() => autoCalibration.Require(Plan(timed: true), "simulation", 4), "axis_cannot_accelerate", "auto_speed_uncalibrated");
        Reject(() => autoCalibration.Require(Plan(), "formal", 4), "formal_cannot_accelerate", "auto_speed_uncalibrated");
        Reject(() => new PcrClanCalibration(2, new HashSet<string> { "simulation" }, null).Require(Plan(), "simulation", 4), "old_profile_no_fast_speed", "auto_speed_uncalibrated");
        var shippedEntry = JObject.Parse(File.ReadAllText(bundledIndex))["entries"]!.OfType<JObject>().FirstOrDefault();
        if (shippedEntry != null)
        {
            var shipped = PcrClanCalibration.Read(sourceRoot, (string?)shippedEntry["content_version"]);
            Check(shipped.Error == null && shipped.AutoBattleSpeeds?.SetEquals(new[] { 1, 2 }) == true, "shipped_opening_and_speed_dependencies_verified");
        }
        Console.WriteLine($"clan_calibration_checks={count}; synthetic fixtures only, game input=0");
    }
}
