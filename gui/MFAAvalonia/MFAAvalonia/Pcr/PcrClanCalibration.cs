using System;
using System.Collections.Generic;
using System.IO;
using System.Globalization;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Pcr;

public sealed record PcrClanCalibration(int Version, IReadOnlySet<string> Capabilities, string? Error, bool OwnUnitsOnly = false, bool AccountStars = false, IReadOnlySet<int>? AutoBattleSpeeds = null, bool ManageInheritance = false)
{
    public const string IndexPath = "assets/resource/clan-battle-calibrations/index.json";
    // Kept with the reviewed Python calibration loader when the bundled index changes.
    public const string IndexSha256 = "62b7e11ca8ef4bb6cee03ca24b35fc9fe17e49eddc88fe6d4d6ca76b165207e0";
    private static readonly string[] Names = { "simulation", "timed", "formal", "carryover" };
    private static readonly string[] Labels = { "模拟战", "秒数轴", "正式三刀", "补偿刀" };
    public bool Supports(string capability) => Error == null && Capabilities.Contains(capability);
    public string Summary => (Error != null ? string.Join(" / ", Labels) + "：不可用。" + Error
        : string.Join("；", Names.Select((name, i) => Labels[i] + "：" + (Supports(name)
            ? name == "timed" ? "测量资料齐全，开始前核对时限" : Version == 1 && name == "simulation" ? "已校准（仅全程 AUTO、无切换）" : "已校准" : "缺少校准"))))
        + (OwnUnitsOnly ? "。仅全自有队伍可用；借人身份尚未校准。" : "")
        + (AccountStars ? " 出战星级使用24小时内的已确认账号资料，调整后须重新同步。" : "");
    public string AutoBattleSummary => AutoBattleSpeeds == null ? "开场设置与倍速：缺少校准。"
        : "开场 SET／AUTO 可逐角色设置；无战中切换时支持 " + string.Join("／", AutoBattleSpeeds.Order()) + " 倍速。"
            + (ManageInheritance ? "自动记录、临时关闭战斗继承，结束后恢复。" : "开始前须临时关闭战斗继承，结束后手动恢复。");

    public static PcrClanCalibration Read(string repository, string? contentVersion) => Read(repository, contentVersion, IndexSha256);

    internal static PcrClanCalibration Read(string repository, string? contentVersion, string expectedIndexSha256)
    {
        try
        {
            if (string.IsNullOrWhiteSpace(repository)) throw new InvalidDataException("程序校准目录未就绪");
            var root = Path.TrimEndingDirectorySeparator(Path.GetFullPath(repository));
            var index = Json(ReadVerified(root, IndexPath, expectedIndexSha256, 1024 * 1024));
            if (!Keys(index, "version", "entries") || index["version"]?.Type != JTokenType.Integer
                || (int?)index["version"] is not (1 or 2) || index["entries"] is not JArray entries || entries.Count > 100)
                throw new InvalidDataException("校准索引格式无效");
            var version = (int)index["version"]!;
            var identities = new HashSet<string>(StringComparer.Ordinal);
            JObject? selected = null;
            foreach (var token in entries)
            {
                var fields = new[] { "calibration_id", "server", "content_version", "frame_size", "profile_path", "profile_sha256" };
                if (token is not JObject entry || !Keys(entry, version == 2 ? fields.Append("capabilities").ToArray() : fields)
                    || entry["calibration_id"]?.Type != JTokenType.String || string.IsNullOrEmpty((string?)entry["calibration_id"])
                    || entry["server"]?.Type != JTokenType.String || (string?)entry["server"] != "cn-bilibili"
                    || entry["content_version"]?.Type != JTokenType.String || !Regex.IsMatch((string)entry["content_version"]!, "\\A[0-9]{12}\\z")
                    || entry["frame_size"] is not JArray size || size.Count != 2 || size.Any(v => v.Type != JTokenType.Integer)
                    || (int?)size[0] != 1280 || (int?)size[1] != 720 || entry["profile_path"]?.Type != JTokenType.String
                    || entry["profile_sha256"]?.Type != JTokenType.String || !Digest((string)entry["profile_sha256"]!))
                    throw new InvalidDataException("校准条目格式无效");
                _ = Local(root, (string)entry["profile_path"]!);
                if (!identities.Add((string)entry["content_version"]!)) throw new InvalidDataException("同一内容版本存在重复校准");
                if (version == 2)
                {
                    if (entry["capabilities"] is not JArray caps || caps.Any(v => v.Type != JTokenType.String || !Names.Contains((string)v))
                        || caps.Values<string>().Distinct(StringComparer.Ordinal).Count() != caps.Count
                        || caps.Values<string>().Contains("carryover") && !caps.Values<string>().Contains("formal"))
                        throw new InvalidDataException("校准能力格式无效");
                }
                if ((string?)entry["content_version"] == contentVersion) selected = entry;
            }
            if (contentVersion == null) throw new InvalidDataException("请先选用有效账号资料");
            if (selected == null) throw new InvalidDataException("当前内容版本缺少随包校准。[clan_battle_calibration_missing]");
            var profile = Json(ReadVerified(root, (string)selected["profile_path"]!, (string)selected["profile_sha256"]!, 4 * 1024 * 1024));
            var capabilities = (version == 1 ? new[] { "simulation" } : selected["capabilities"]!.Values<string>()).ToHashSet(StringComparer.Ordinal);
            if (profile["version"]?.Type != JTokenType.Integer || (int?)profile["version"] is not (1 or 2)
                || profile["evidence_kind"]?.Type != JTokenType.String || (string?)profile["evidence_kind"] != "device_saved_frame"
                || (string?)profile["calibration_id"] != (string?)selected["calibration_id"] || (string?)profile["server"] != "cn-bilibili"
                || !JToken.DeepEquals(profile["frame_size"], selected["frame_size"])) throw new InvalidDataException("校准档案与索引不符");
            if ((int?)profile["version"] == 1 && capabilities.Any(cap => cap != "simulation"))
                throw new InvalidDataException("旧版模拟校准不能授予秒数轴、正式或补偿能力");
            var ownUnitsOnly = profile.ContainsKey("support_modes");
            if (ownUnitsOnly && ((int?)profile["version"] != 2 || !JToken.DeepEquals(profile["support_modes"], new JArray("none"))))
                throw new InvalidDataException("校准支援范围无效。[clan_battle_calibration_support_scope_invalid]");
            var starSource = profile["formation"]?["star_source"];
            if (starSource != null && ((int?)profile["version"] != 2 || !ownUnitsOnly || (string?)starSource != "account_snapshot"))
                throw new InvalidDataException("星级资料来源无效。[clan_battle_calibration_star_source_invalid]");
            VerifyDependencies(root, profile, capabilities, ownUnitsOnly);
            return new(version == 1 ? 1 : (int)profile["version"]!, capabilities, null, ownUnitsOnly, starSource != null,
                profile["auto_battle"] is JObject autoBattle ? autoBattle["speeds"]!.Values<int>().ToHashSet() : null,
                profile["auto_battle"]?["inheritance"] is JObject);
        }
        catch (Exception ex) when (ex is IOException or InvalidDataException or UnauthorizedAccessException or ArgumentException
            or InvalidCastException or FormatException or OverflowException or Newtonsoft.Json.JsonException)
        { return new(0, new HashSet<string>(StringComparer.Ordinal), ex.Message); }
    }

    public void Require(JObject plan, string capability, int battleSpeed = 1)
    {
        if (!Names.Contains(capability)) throw new InvalidDataException("未知会战校准能力。");
        if (!Supports(capability)) throw new InvalidOperationException((Error ?? "缺少" + Labels[Array.IndexOf(Names, capability)] + "校准")
            + " [clan_battle_calibration_capability_missing:" + capability + "]");
        if (Version == 1 && ((bool?)plan["auto"] != true || plan["switches"] is not JArray { Count: 0 }))
            throw new InvalidOperationException("此校准只支持全程 AUTO、无战中切换。[clan_battle_calibration_auto_only]");
        if (plan["switches"] is JArray { Count: > 0 } && !Supports("timed"))
            throw new InvalidOperationException("当前内容版本缺少秒数轴校准。[clan_battle_calibration_capability_missing:timed]");
        if (OwnUnitsOnly && (string?)plan["support_mode"] != "none")
            throw new InvalidOperationException("仅全自有队伍可用；借人身份尚未校准。[clan_battle_support_mode_uncalibrated]");
        if (battleSpeed is not (1 or 2 or 4)) throw new InvalidOperationException("会战倍速无效。[clan_battle_speed_invalid]");
        if (battleSpeed != 1 && (capability != "simulation" || plan["switches"] is not JArray { Count: 0 }
                || AutoBattleSpeeds?.Contains(battleSpeed) != true))
            throw new InvalidOperationException("此作业或内容版本尚未校准所选倍速；秒数轴只支持 1 倍速。[clan_battle_auto_speed_uncalibrated]");
        if (AccountStars) RequireAccountStars(plan);
    }

    private static void RequireAccountStars(JObject plan)
    {
        var binding = (JObject)plan["snapshot"]!;
        var path = (string)binding["path"]!;
        var info = new FileInfo(path);
        if (!info.Exists || info.Length is < 1 or > 16 * 1024 * 1024)
            throw new InvalidDataException("账号资料缺失。[clan_battle_source_file_missing]");
        var bytes = File.ReadAllBytes(path);
        if (PcrClanGuide.Hash(bytes) != (string?)binding["sha256"])
            throw new InvalidDataException("账号资料已变化，请重新核对保存。[clan_battle_source_changed]");
        var snapshot = Json(bytes);
        if ((int?)snapshot["schema_version"] != 1 || new[] { "server", "account_id", "content_version" }
                .Any(key => !JToken.DeepEquals(snapshot[key], plan[key])))
            throw new InvalidDataException("账号资料与作业不一致。[clan_battle_snapshot_identity_mismatch]");
        var observedText = (string?)snapshot["observed_at"];
        if (observedText == null || !Regex.IsMatch(observedText, "(?:Z|[+-][0-9]{2}:[0-9]{2})$")
            || !DateTimeOffset.TryParse(observedText, CultureInfo.InvariantCulture,
                DateTimeStyles.None, out var observed) || (DateTimeOffset.UtcNow - observed).TotalHours is < 0 or > 24)
            throw new InvalidOperationException("账号资料超过24小时或时间异常，请重新读取账号资料并保存作业。[clan_battle_snapshot_expired]");
        if ((string?)plan["support_mode"] != "none")
            throw new InvalidOperationException("账号星级资料不包含支援校验。[clan_battle_account_stars_support_uncalibrated]");
        var unknown = snapshot["unknown_fields"]!.Values<string>().ToArray();
        var units = snapshot["units"]!.OfType<JObject>().ToDictionary(unit => (int)unit["unit_id"]!);
        var ids = plan["unit_ids"]!.Values<int>().ToArray();
        for (var index = 0; index < ids.Length; index++)
        {
            var key = $"units.{ids[index]}.battle_rarity";
            if (!units.TryGetValue(ids[index], out var unit) || unit["battle_rarity"]?.Type != JTokenType.Integer
                || (int)unit["battle_rarity"]! is < 1 or > 6
                || unknown.Any(field => key == field || key.StartsWith(field + ".", StringComparison.Ordinal)))
                throw new InvalidOperationException("账号资料缺少出战星级。[clan_battle_account_stars_unknown]");
            if (!JToken.DeepEquals(unit["battle_rarity"], plan["stars"]![index]))
                throw new InvalidOperationException("当前出战星级与作业不符，请先在游戏调整后重新同步。[clan_battle_account_stars_mismatch]");
        }
    }

    private static bool Keys(JObject value, params string[] names) => value.Properties().Select(p => p.Name).ToHashSet(StringComparer.Ordinal).SetEquals(names);
    private static bool Digest(string value) => Regex.IsMatch(value, "\\A[0-9a-f]{64}\\z");
    private static JObject Json(byte[] bytes)
    {
        using var reader = new JsonTextReader(new StringReader(new UTF8Encoding(false, true).GetString(bytes))) { DateParseHandling = DateParseHandling.None };
        return JObject.Load(reader, new JsonLoadSettings { DuplicatePropertyNameHandling = DuplicatePropertyNameHandling.Error });
    }
    private static void VerifyDependencies(string root, JObject profile, IReadOnlySet<string> capabilities, bool ownUnitsOnly)
    {
        if (profile["battle_hud"]?.Type != JTokenType.String || (string?)profile["battle_hud"] != "guide-trial-1280x720"
            || new[] { "pages", "actions", "formation" }.Any(key => profile[key] is not JObject)
            || !ownUnitsOnly && profile["support"] is not JObject)
            throw new InvalidDataException("校准档案缺少必填结构。[clan_battle_calibration_not_reviewed_device_profile]");
        var sections = new List<JObject> { profile };
        if (profile.ContainsKey("auto_battle"))
        {
            if ((int?)profile["version"] != 2 || profile["auto_battle"] is not JObject autoBattle
                || !Keys(autoBattle, new[] { "speeds", "opening_guards", "paused_guards", "auto_on", "auto_off", "speed", "set_badge", "empty_tp", "samples" }
                    .Concat(autoBattle.ContainsKey("inheritance") ? new[] { "inheritance" } : Array.Empty<string>()).ToArray())
                || autoBattle["speeds"] is not JArray { Count: > 0 } speeds || !speeds.Any(v => v.Type == JTokenType.Integer && (int)v == 1)
                || speeds.Any(v => v.Type != JTokenType.Integer || (int)v is not (1 or 2 or 4)) || speeds.Values<int>().Distinct().Count() != speeds.Count
                || autoBattle["opening_guards"] is not JArray { Count: 3 } || autoBattle["paused_guards"] is not JArray { Count: 4 }
                || autoBattle["speed"] is not JObject speed || !Keys(speed, "1", "2", "4")
                || new[] { "auto_on", "auto_off", "set_badge", "empty_tp" }.Any(key => autoBattle[key] is not JObject))
                throw new InvalidDataException("开场设置校准无效。[clan_battle_auto_calibration_invalid]");
            if (autoBattle["paused_guards"]![2] is not JObject clock || (string?)clock["recognition"] != "TemplateMatch"
                || clock["template"] is not JArray { Count: 2 } || clock["sha256"] is not JArray { Count: 2 })
                throw new InvalidDataException("开场时钟校准无效。[clan_battle_auto_clock_calibration_invalid]");
            if (speeds.Values<int>().Contains(2) && profile["pages"]?["battle"]?["speed_two"] is not JObject
                || speeds.Values<int>().Contains(4) && profile["pages"]?["battle"]?["speed_four"] is not JObject)
                throw new InvalidDataException("所选倍速缺少战斗画面校准。[clan_battle_auto_speed_calibration_missing]");
            var inheritanceMarkers = Enumerable.Empty<JToken>();
            if (autoBattle.ContainsKey("inheritance"))
            {
                if (autoBattle["inheritance"] is not JObject inheritance || !Keys(inheritance, "battle_selected", "radio_on", "radio_off", "samples")
                    || new[] { "battle_selected", "radio_on", "radio_off" }.Any(key => inheritance[key] is not JObject node
                        || (string?)node["recognition"] != "TemplateMatch"))
                    throw new InvalidDataException("战斗继承校准无效。[clan_battle_inheritance_calibration_invalid]");
                inheritanceMarkers = new[] { inheritance["battle_selected"]!, inheritance["radio_on"]!, inheritance["radio_off"]! };
                sections.Add(inheritance);
            }
            foreach (var marker in ((JArray)autoBattle["opening_guards"]!).Concat((JArray)autoBattle["paused_guards"]!)
                .Concat(speed.Properties().Select(p => p.Value)).Concat(new[] { autoBattle["auto_on"]!, autoBattle["auto_off"]!, autoBattle["set_badge"]!, autoBattle["empty_tp"]! }).Concat(inheritanceMarkers))
            {
                if (marker is not JObject node || (string?)node["recognition"] is not ("OCR" or "TemplateMatch")
                    || node["roi"] is not JArray { Count: 4 } roi || roi.Any(v => v.Type != JTokenType.Integer)
                    || (int)roi[0] < 0 || (int)roi[1] < 0 || (int)roi[2] <= 0 || (int)roi[3] <= 0
                    || (long)roi[0] + (long)roi[2] > 1280 || (long)roi[1] + (long)roi[3] > 720
                    || (string?)node["recognition"] == "OCR" && (node["expected"]?.Type != JTokenType.String
                        || !((string)node["expected"]!).StartsWith('^') || !((string)node["expected"]!).EndsWith('$')))
                    throw new InvalidDataException("开场设置页面锚点无效。[clan_battle_auto_calibration_invalid]");
            }
            sections.Add(autoBattle);
        }
        if (profile.ContainsKey("daily"))
        {
            if (profile["daily"] is not JObject daily) throw new InvalidDataException("正式校准结构无效。[clan_battle_daily_calibration_invalid]");
            sections.Add(daily);
            if (daily.ContainsKey("execution"))
            {
                if (daily["execution"] is not JObject execution) throw new InvalidDataException("正式执行校准缺失。[clan_battle_daily_execution_calibration_missing]");
                sections.Add(execution);
            }
        }
        if (capabilities.Contains("formal") || capabilities.Contains("carryover"))
        {
            if (profile["daily"] is not JObject daily || daily["version"]?.Type != JTokenType.Integer || (int?)daily["version"] != 2
                || daily["execution"] is not JObject execution || !Keys(execution, "pages", "actions", "used_units", "carry_slots", "boss_slots", "formation_owner", "formation_mode", "speed_one", "samples"))
                throw new InvalidDataException("正式执行校准字段不完整。[clan_battle_daily_execution_calibration_missing]");
        }
        foreach (var section in sections)
        {
            if (section["samples"] is not JArray { Count: > 0 } samples) throw new InvalidDataException("校准样本缺失。[clan_battle_calibration_samples_missing]");
            foreach (var token in samples)
            {
                if (token is not JObject sample || !Keys(sample, "page", "path", "sha256") || sample["page"]?.Type != JTokenType.String
                    || sample["path"]?.Type != JTokenType.String || sample["sha256"]?.Type != JTokenType.String)
                    throw new InvalidDataException("校准样本引用无效。[clan_battle_calibration_sample_invalid]");
                _ = ReadVerified(root, (string)sample["path"]!, (string)sample["sha256"]!, 32 * 1024 * 1024);
            }
        }
        foreach (var spec in profile.Descendants().OfType<JObject>().Prepend(profile).Where(value => (string?)value["recognition"] == "TemplateMatch"))
        {
            if ((spec["template"] is JArray) != (spec["sha256"] is JArray))
                throw new InvalidDataException("校准模板摘要数量不一致。[clan_battle_calibration_template_invalid]");
            var templates = spec["template"] as JArray ?? new JArray(spec["template"]);
            var hashes = spec["sha256"] as JArray ?? new JArray(spec["sha256"]);
            if (templates.Count is < 1 or > 16 || templates.Count != hashes.Count
                || templates.Any(value => value.Type != JTokenType.String) || hashes.Any(value => value.Type != JTokenType.String))
                throw new InvalidDataException("校准模板引用无效。[clan_battle_calibration_template_invalid]");
            for (var index = 0; index < templates.Count; index++)
                _ = ReadVerified(root, "assets/resource/image/" + (string)templates[index]!, (string)hashes[index]!, 4 * 1024 * 1024);
        }
        if (capabilities.Contains("timed"))
        {
            if (profile["timing_measurements"] is not JObject binding || !Keys(binding, "path", "sha256")
                || binding["path"]?.Type != JTokenType.String || binding["sha256"]?.Type != JTokenType.String)
                throw new InvalidDataException("秒数轴测量文件缺失。[clan_battle_calibration_timing_missing]");
            var measured = Json(ReadVerified(root, (string)binding["path"]!, (string)binding["sha256"]!, 1024 * 1024));
            if ((string?)measured["kind"] != "owner_native_clan_timing" || !JToken.DeepEquals(measured["calibration_id"], profile["calibration_id"])
                || !JToken.DeepEquals(measured["battle_hud"], profile["battle_hud"]) || (string?)measured["observation_scope"] != "full_read_tick"
                || (string?)measured["action_scope"] != "toggle_dispatch_and_full_team_readback" || measured["battle_speed"]?.Type != JTokenType.Integer || (int?)measured["battle_speed"] != 1
                || new[] { "observation_intervals_ms", "action_latencies_ms", "game_second_ms" }.Any(key => measured[key] is not JArray { Count: >= 20 } values
                    || values.Any(value => value.Type != JTokenType.Integer || (long)value <= 0)))
                throw new InvalidDataException("秒数轴测量字段或绑定不完整。[clan_battle_timing_not_calibrated]");
        }
    }
    private static string Local(string root, string relative)
    {
        if (string.IsNullOrWhiteSpace(relative) || relative.Contains('\\') || relative.Contains(':') || Path.IsPathRooted(relative)
            || relative.Split('/').Any(part => part is ".." or "." or "")) throw new InvalidDataException("校准路径无效");
        var path = Path.GetFullPath(Path.Combine(root, relative));
        for (var current = path; current != root; current = Path.GetDirectoryName(current)!)
            if ((File.Exists(current) || Directory.Exists(current)) && (File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
                throw new InvalidDataException("校准路径不能包含链接");
        return path;
    }
    private static byte[] ReadVerified(string root, string relative, string digest, int limit)
    {
        var path = Local(root, relative);
        if (!Digest(digest)) throw new InvalidDataException("校准文件摘要格式无效。[clan_battle_calibration_digest_invalid]");
        var info = new FileInfo(path);
        if (!info.Exists || info.Length is < 1 || info.Length > limit)
            throw new InvalidDataException("随包校准文件缺失或大小无效：" + relative + " [clan_battle_calibration_file_missing]");
        var bytes = File.ReadAllBytes(path);
        if (bytes.Length > limit || PcrClanGuide.Hash(bytes) != digest) throw new InvalidDataException("随包校准文件校验失败：" + relative + " [clan_battle_calibration_file_changed]");
        return bytes;
    }
}
