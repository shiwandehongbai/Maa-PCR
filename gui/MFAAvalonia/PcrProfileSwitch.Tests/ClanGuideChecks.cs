using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Media.Imaging;
using Avalonia.Threading;
using MFAAvalonia.Pcr;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

internal static class ClanGuideChecks
{
    internal static void CompleteLoading(PcrClanGuideView view)
    {
        var clock = System.Diagnostics.Stopwatch.StartNew();
        while (clock.Elapsed < TimeSpan.FromSeconds(20))
        {
            Dispatcher.UIThread.RunJobs();
            if (view.PendingLoad.IsCompleted)
            {
                Dispatcher.UIThread.RunJobs();
                if (view.PendingLoad.IsCompleted) { view.PendingLoad.GetAwaiter().GetResult(); return; }
            }
            System.Threading.Thread.Sleep(5);
        }
        throw new TimeoutException("clan_guide_async_load_did_not_finish");
    }
    public static void Run(string directory, string python)
    {
        var root = Path.GetFullPath(Path.Combine(directory, "clan-guide")); Directory.CreateDirectory(root);
        var config = Path.Combine(root, "config"); Directory.CreateDirectory(config);
        var now = DateTimeOffset.Now; var count = 0;
        void Require(bool value, string name) { if (!value) throw new Exception("clan_guide_" + name); count++; }
        void Throws(Action action, string name) { try { action(); } catch { count++; return; } throw new Exception("clan_guide_expected_rejection_" + name); }
        PcrClanGuideDocument Copy(PcrClanGuideDocument value) => JsonConvert.DeserializeObject<PcrClanGuideDocument>(JsonConvert.SerializeObject(value))!;
        var catalog = new PcrGuideCatalog { Server = "cn-bilibili", ContentVersion = "202610141431", Source = "synthetic offline fixture", Sha256 = new string('a', 64),
            Units = Enumerable.Range(1, 6).Select(i => new PcrGuideUnit(100001 + i * 100, "合成角色" + i)).ToList() };
        var legacy = Path.Combine(config, "guide-review.json");
        var oldStore = new PcrGuideStore(legacy); oldStore.Load(); oldStore.Save(new() { Input = new() { Catalog = catalog, Title = "原深域草稿" } });
        var originalLegacy = File.ReadAllBytes(legacy);
        var snapshot = new JObject { ["schema_version"] = 1, ["server"] = "cn-bilibili", ["account_id"] = "123456", ["content_version"] = catalog.ContentVersion,
            ["observed_at"] = now.AddMinutes(-5).ToString("O"), ["ownership_complete"] = true, ["unknown_fields"] = new JArray(),
            ["units"] = new JArray(catalog.Units.Take(5).Select(u => new JObject { ["unit_id"] = u.UnitId, ["name"] = u.Name, ["battle_rarity"] = 5 })) };
        var cache = new PcrGuideAccountStore(config); cache.Save(Encoding.UTF8.GetBytes(snapshot.ToString()), catalog, "123456", now, null);
        var snapshotPath = PcrClanGuide.SnapshotPaths(config).Single(); var originalSnapshot = File.ReadAllBytes(snapshotPath);
        var imagePath = Path.Combine(root, "synthetic-source.png");
        using (var bitmap = new RenderTargetBitmap(new PixelSize(8, 8), new Vector(96, 96))) bitmap.Save(imagePath);
        var originalImage = File.ReadAllBytes(imagePath);
        var ids = catalog.Units.Take(5).Select(u => u.UnitId).ToArray();
        JObject Job(string id, int page, string package) => new()
        {
            ["period"] = "2026-09", ["phase"] = page == 1 ? "BC" : "D", ["round_min"] = page == 1 ? 1 : 23, ["round_max"] = page == 1 ? new JValue(22) : JValue.CreateNull(),
            ["boss"] = page == 1 ? 1 : 5, ["boss_name"] = "合成 BOSS", ["package"] = package, ["team_label"] = "合成队伍" + id,
            ["unit_ids"] = new JArray(ids), ["stars"] = new JArray(ids.Select(_ => JValue.CreateNull())), ["auto"] = true,
            ["set_by_unit"] = new JObject(ids.Select((uid, index) => new JProperty(uid.ToString(), index % 2 == 0))),
            ["switches"] = new JArray(), ["support_mode"] = "none", ["support_unit_id"] = JValue.CreateNull(), ["reference_damage"] = 3200000000L, ["image_base64"] = Convert.ToBase64String(originalImage),
            ["source"] = new JObject { ["url"] = "https://www.bilibili.com/video/BV1Rbh86XEsq/" + (page > 1 ? "?p=" + page : ""), ["bvid"] = "BV1Rbh86XEsq", ["page"] = page,
                ["cid"] = page == 1 ? 42138340822L : 42155640154L, ["at_seconds"] = 12, ["image_path"] = imagePath, ["image_sha256"] = PcrClanGuide.Hash(originalImage), ["job_id"] = id }
        };
        var bundle = new JObject { ["version"] = 1, ["source"] = new JObject { ["url"] = "https://www.bilibili.com/video/BV1Rbh86XEsq/", ["bvid"] = "BV1Rbh86XEsq", ["title"] = "合成离线样例", ["author"] = "fixture", ["period"] = "2026-09" },
            ["pages"] = new JArray(new JObject { ["page"] = 1, ["cid"] = 42138340822L, ["label"] = "BC" }, new JObject { ["page"] = 2, ["cid"] = 42155640154L, ["label"] = "D" }),
            ["jobs"] = new JArray(Job("bc-a", 1, "套餐甲"), Job("bc-b", 1, "套餐乙"), Job("d-a", 2, "套餐甲")) };
        var candidateJob = Job("candidate", 1, "待核对候选"); candidateJob["unit_ids"] = new JArray(ids.Select(_ => JValue.CreateNull())); candidateJob["set_by_unit"] = new JObject();
        candidateJob["members"] = new JArray(ids.Select((id, index) => new JObject { ["unit_id"] = JValue.CreateNull(), ["proposal_unit_id"] = id, ["proposal_name"] = catalog.Units[index].Name, ["set"] = index % 2 == 0 }));
        ((JArray)bundle["jobs"]!).Add(candidateJob);
        var unknownAxisJob = Job("unknown-axis", 1, "未知原轴"); unknownAxisJob["switches"] = JValue.CreateNull(); ((JArray)bundle["jobs"]!).Add(unknownAxisJob);
        var bundlePath = Path.Combine(root, "bundle.json"); File.WriteAllText(bundlePath, bundle.ToString(), new UTF8Encoding(false)); var originalBundle = File.ReadAllBytes(bundlePath);
        var statePath = Path.Combine(config, "agent-guide-review.json");
        var document = new PcrClanGuideDocument { SnapshotPath = snapshotPath, SnapshotSha256 = PcrClanGuide.Hash(originalSnapshot) };
        PcrClanGuide.Select(document, bundlePath, "bc-a");
        Require(document.ConfirmedFor == null, "import_never_confirms");
        var plan = PcrClanGuide.BuildPlan(document, config, root, now);
        Require((long)plan["source"]!["cid"]! == 42138340822L && (long)plan["reference_damage"]! == 3200000000L, "large_cid_and_damage");
        Require(plan["stars"]!.Values<int>().All(x => x == 5), "missing_guide_stars_use_snapshot_battle_rarity");
        Require(PcrClanGuide.Fingerprint(JObject.Parse("{\"b\":1,\"a\":[true,\"雪\"]}")) == PcrClanGuide.Hash(Encoding.UTF8.GetBytes("{\"a\":[true,\"雪\"],\"b\":1}")), "canonical_ordinal_utf8");
        PcrClanGuide.Save(document, statePath, null, config, root, now, false);
        Throws(() => PcrClanGuide.CreatePlan(statePath, config, root, now), "draft_cannot_start");
        PcrClanGuide.Save(document, statePath, PcrClanGuide.Stamp(statePath), config, root, now, true);
        var reopened = PcrClanGuide.Read(statePath);
        _ = PcrClanGuide.Read(statePath, out var readStamp); Require(readStamp == PcrClanGuide.Stamp(statePath), "read_hash_belongs_to_loaded_bytes");
        Require(JToken.DeepEquals(plan, PcrClanGuide.CreatePlan(statePath, config, root, now)), "confirmed_save_reopen");
        Require(reopened.Job["stars"]!.All(s => s.Type == JTokenType.Null), "snapshot_fallback_does_not_rewrite_guide_reference");
        Require(reopened.Job["image_base64"] == null, "selection_references_original_media_without_copy");
        var edited = Copy(reopened); edited.Job["boss"] = 2;
        File.WriteAllText(statePath, JsonConvert.SerializeObject(edited));
        Throws(() => PcrClanGuide.CreatePlan(statePath, config, root, now), "target_change_invalidates_confirmation");
        PcrClanGuide.Save(reopened, statePath, PcrClanGuide.Stamp(statePath), config, root, now, true);
        var firstStamp = PcrClanGuide.Stamp(statePath); var concurrent = Copy(reopened); concurrent.Job["team_label"] = "另一个窗口";
        PcrClanGuide.Save(concurrent, statePath, firstStamp, config, root, now, false); var kept = File.ReadAllBytes(statePath);
        Throws(() => PcrClanGuide.Save(reopened, statePath, firstStamp, config, root, now, true), "concurrent_save");
        Require(File.ReadAllBytes(statePath).SequenceEqual(kept), "concurrent_save_preserves_original");
        PcrClanGuide.Save(reopened, statePath, PcrClanGuide.Stamp(statePath), config, root, now, true);
        var cross = Copy(reopened); PcrClanGuide.Select(cross, bundlePath, "bc-b");
        Require(cross.ConfirmedFor == null && (string?)cross.Job["package"] == "套餐乙", "package_change_clears_confirmation");
        PcrClanGuide.Select(cross, bundlePath, "d-a");
        Require((int)PcrClanGuide.BuildPlan(cross, config, root, now)["source"]!["page"]! == 2 && (string?)cross.Job["phase"] == "D", "cross_page_separate_phase");
        var multiplePath = Path.Combine(config, "multiple-state.json"); var multiple = Copy(reopened); multiple.Job["boss"] = 2;
        var multipleHash = PcrClanGuide.Save(multiple, multiplePath, null, config, root, now, true);
        PcrClanGuide.Select(multiple, bundlePath, "d-a"); multipleHash = PcrClanGuide.Save(multiple, multiplePath, multipleHash, config, root, now, true);
        PcrClanGuide.Select(multiple, bundlePath, "bc-a");
        Require((int)multiple.Job["boss"]! == 2 && multiple.ConfirmedFor == (string?)PcrClanGuide.BuildPlan(multiple, config, root, now)["confirmed_for"], "bc_d_bc_restores_reviewed_changes_and_confirmation");
        multipleHash = PcrClanGuide.Save(multiple, multiplePath, multipleHash, config, root, now, true);
        var multipleReopened = PcrClanGuide.Read(multiplePath); PcrClanGuide.Select(multipleReopened, bundlePath, "d-a"); PcrClanGuide.Select(multipleReopened, bundlePath, "bc-a");
        Require((int)multipleReopened.Job["boss"]! == 2 && multipleReopened.ConfirmedFor != null, "saved_team_revisions_survive_reopen");
        multipleReopened.SnapshotSha256 = new string('0', 64); PcrClanGuide.Select(multipleReopened, bundlePath, "bc-a");
        Require(multipleReopened.ConfirmedFor == null, "recalled_team_new_snapshot_clears_confirmation");
        void Reject(Action<PcrClanGuideDocument> change, string name)
        { var bad = Copy(reopened); change(bad); Throws(() => PcrClanGuide.BuildPlan(bad, config, root, now), name); }
        Reject(d => d.Job["boss"] = JValue.CreateNull(), "unknown_boss");
        Reject(d => d.Job["unit_ids"]![0] = d.Job["unit_ids"]![1]!.DeepClone(), "duplicate_member");
        Reject(d => d.Job["set_by_unit"]![ids[0].ToString()] = JValue.CreateNull(), "unknown_set_never_default_true");
        Reject(d => d.Job["auto"] = JValue.CreateNull(), "unknown_auto");
        Reject(d => d.Job["source"]!["page"] = 2, "source_cannot_be_rebound_by_edit");
        Reject(d => d.Job["stars"]![0] = 7, "invalid_star");
        Reject(d => d.Job["support_mode"] = "specified", "specified_support_required");
        Reject(d => d.SnapshotSha256 = new string('0', 64), "snapshot_changed");
        var qsjs = Copy(reopened); qsjs.Job["support_mode"] = "qsjs";
        Require((string?)PcrClanGuide.BuildPlan(qsjs, config, root, now)["support_mode"] == "qsjs", "qsjs_zero_missing_keeps_own");
        qsjs.Job["unit_ids"]![4] = catalog.Units[5].UnitId; qsjs.Job["stars"]![4] = 5;
        ((JObject)qsjs.Job["set_by_unit"]!).Remove(ids[4].ToString()); qsjs.Job["set_by_unit"]![catalog.Units[5].UnitId.ToString()] = true;
        var qsjsPlan = PcrClanGuide.BuildPlan(qsjs, config, root, now);
        Require((int)qsjsPlan["unit_ids"]![4]! == catalog.Units[5].UnitId, "qsjs_one_missing_exact_identity");
        var specified = Copy(qsjs); specified.Job["support_mode"] = "specified"; specified.Job["support_unit_id"] = catalog.Units[5].UnitId;
        var specifiedPlan = PcrClanGuide.BuildPlan(specified, config, root, now);
        Require((int)specifiedPlan["support_unit_id"]! == catalog.Units[5].UnitId, "specified_support");
        var incomplete = (JObject)snapshot.DeepClone(); incomplete["ownership_complete"] = false; var incompleteBytes = Encoding.UTF8.GetBytes(incomplete.ToString());
        File.WriteAllBytes(snapshotPath, incompleteBytes); var partial = Copy(qsjs); partial.SnapshotSha256 = PcrClanGuide.Hash(incompleteBytes);
        Throws(() => PcrClanGuide.BuildPlan(partial, config, root, now), "partial_absence_remains_unknown");
        var partialOwned = Copy(reopened); partialOwned.SnapshotSha256 = PcrClanGuide.Hash(incompleteBytes);
        Require((string?)PcrClanGuide.BuildPlan(partialOwned, config, root, now)["account_id"] == "123456", "partial_all_five_explicitly_owned");
        var partialSupport = Copy(specified); partialSupport.SnapshotSha256 = PcrClanGuide.Hash(incompleteBytes);
        Require((int)PcrClanGuide.BuildPlan(partialSupport, config, root, now)["support_unit_id"]! == catalog.Units[5].UnitId, "partial_four_owned_plus_specified_support");
        incomplete["unknown_fields"] = new JArray("units." + catalog.Units[5].UnitId); var unknownSupportBytes = Encoding.UTF8.GetBytes(incomplete.ToString());
        File.WriteAllBytes(snapshotPath, unknownSupportBytes); partialSupport.SnapshotSha256 = PcrClanGuide.Hash(unknownSupportBytes);
        Require((int)PcrClanGuide.BuildPlan(partialSupport, config, root, now)["support_unit_id"]! == catalog.Units[5].UnitId, "specified_borrowed_row_unknown_is_not_own_requirement");
        incomplete["unknown_fields"] = new JArray("units." + ids[0]); var unknownOwnedBytes = Encoding.UTF8.GetBytes(incomplete.ToString());
        File.WriteAllBytes(snapshotPath, unknownOwnedBytes); partialSupport.SnapshotSha256 = PcrClanGuide.Hash(unknownOwnedBytes);
        Throws(() => PcrClanGuide.BuildPlan(partialSupport, config, root, now), "specified_own_member_unknown_stops");
        incomplete["unknown_fields"] = new JArray("units." + ids[0] + ".ownership"); var unknownOwnershipBytes = Encoding.UTF8.GetBytes(incomplete.ToString());
        File.WriteAllBytes(snapshotPath, unknownOwnershipBytes); partialSupport.SnapshotSha256 = PcrClanGuide.Hash(unknownOwnershipBytes);
        Throws(() => PcrClanGuide.BuildPlan(partialSupport, config, root, now), "ownership_leaf_unknown_stops");
        incomplete["unknown_fields"] = new JArray("units." + ids[0] + ".battle_rarity"); var unknownStarBytes = Encoding.UTF8.GetBytes(incomplete.ToString());
        File.WriteAllBytes(snapshotPath, unknownStarBytes); partialSupport.SnapshotSha256 = PcrClanGuide.Hash(unknownStarBytes);
        Throws(() => PcrClanGuide.BuildPlan(partialSupport, config, root, now), "unknown_star_value_cannot_be_snapshot_fallback");
        partialSupport.Job["stars"]![0] = 5;
        Require(PcrClanGuide.BuildPlan(partialSupport, config, root, now)["stars"]![0]!.Value<int>() == 5, "explicit_guide_star_does_not_claim_unknown_snapshot_value");
        var missingTwo = (JObject)snapshot.DeepClone(); missingTwo["units"] = new JArray(((JArray)missingTwo["units"]!).Take(3));
        var missingTwoBytes = Encoding.UTF8.GetBytes(missingTwo.ToString()); File.WriteAllBytes(snapshotPath, missingTwoBytes);
        var qsjsMissingTwo = Copy(reopened); qsjsMissingTwo.SnapshotSha256 = PcrClanGuide.Hash(missingTwoBytes); qsjsMissingTwo.Job["support_mode"] = "qsjs"; qsjsMissingTwo.Job["stars"] = new JArray(ids.Select(_ => 5));
        Throws(() => PcrClanGuide.BuildPlan(qsjsMissingTwo, config, root, now), "qsjs_two_missing_stops");
        File.WriteAllBytes(snapshotPath, originalSnapshot);
        var timed = Copy(reopened); timed.Job["switches"] = new JArray(new JObject { ["remaining_seconds"] = 75, ["auto"] = JValue.CreateNull(), ["set_by_unit"] = new JObject { [ids[0].ToString()] = false } },
            new JObject { ["remaining_seconds"] = 50, ["auto"] = false, ["set_by_unit"] = new JObject() });
        var timedPlan = PcrClanGuide.BuildPlan(timed, config, root, now);
        Require(timedPlan["switches"]!.Count() == 2, "explicit_seconds_axis");
        timed.Job["switches"]![1]!["remaining_seconds"] = 75;
        Throws(() => PcrClanGuide.BuildPlan(timed, config, root, now), "duplicate_axis_second");
        File.WriteAllBytes(imagePath, originalImage.Concat(new byte[] { 0 }).ToArray());
        Throws(() => PcrClanGuide.BuildPlan(reopened, config, root, now), "concurrent_image_change"); File.WriteAllBytes(imagePath, originalImage);
        File.WriteAllText(bundlePath, bundle.ToString() + "\n");
        Throws(() => PcrClanGuide.BuildPlan(reopened, config, root, now), "concurrent_bundle_change"); File.WriteAllBytes(bundlePath, originalBundle);
        Throws(() => PcrClanGuide.BuildPlan(reopened, config, root, now.AddHours(25)), "expired_snapshot");
        var publicBefore = Directory.Exists(Path.Combine(config, "guide-library")) ? Directory.GetFiles(Path.Combine(config, "guide-library"), "*.json") : Array.Empty<string>();
        PcrClanGuide.SavePublicGuide(reopened, config, root);
        var library = new PcrGuideLibrary(Path.Combine(config, "guide-library")); library.Reload();
        Require(library.Entries.Count >= 1 && library.Entries.Any(e => e.Label.Contains("公会战")), "shared_public_guide_library");
        var publicText = Encoding.UTF8.GetString(library.Read(library.Entries.First(e => e.Label.Contains("公会战")).Key));
        Require(!publicText.Contains("123456") && !publicText.Contains(snapshotPath.Replace("\\", "\\\\")), "library_has_no_account_snapshot");
        var publicPath = Directory.GetFiles(Path.Combine(config, "guide-library"), "*.json").Single(); var publicOriginal = File.ReadAllBytes(publicPath);
        var changedPublic = Copy(reopened); changedPublic.Job["set_by_unit"]![ids[0].ToString()] = false; PcrClanGuide.SavePublicGuide(changedPublic, config, root);
        Require(File.ReadAllBytes(publicPath).SequenceEqual(publicOriginal), "existing_public_entry_not_overwritten");
        changedPublic.Job["source"]!["url"] = "https://www.bilibili.com/video/BV1Xunyz2E21/"; PcrClanGuide.SavePublicGuide(changedPublic, config, root); library.Reload();
        Require(library.Entries.Count == 2, "same_label_distinct_source_saved_as_separate_public_guide");
        Require(File.ReadAllBytes(legacy).SequenceEqual(originalLegacy) && File.ReadAllBytes(snapshotPath).SequenceEqual(originalSnapshot), "original_shared_files_unchanged");
        var view = new PcrClanGuideView(root, statePath, config); Dispatcher.UIThread.RunJobs();
        Require(view.HasCurrentConfirmation, "ui_reopen_confirmation");
        var boxes = (Dictionary<string, TextBox>)typeof(PcrClanGuideView).GetField("fields", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(view)!;
        boxes["boss"].Text = "2"; Dispatcher.UIThread.RunJobs();
        Require(!view.HasCurrentConfirmation && view.Capture().ConfirmedFor == null, "ui_edit_clears_confirmation");
        view.SaveDraft(); Require(PcrClanGuide.Read(statePath).ConfirmedFor == null, "ui_saves_only_draft_after_edit");
        view.Display(reopened); view.ConfirmAndSave(); Require(view.HasCurrentConfirmation, "ui_once_confirm");
        var second = new PcrClanGuideView(root, statePath, config); Dispatcher.UIThread.RunJobs();
        Require(second.HasCurrentConfirmation, "ui_save_reopen");
        var refreshedSnapshot = (JObject)snapshot.DeepClone(); refreshedSnapshot["observed_at"] = now.ToString("O");
        var savedGuideBytes = File.ReadAllBytes(statePath); var beforeRefreshJob = second.Capture().Job.ToString();
        try
        {
            cache.Save(Encoding.UTF8.GetBytes(refreshedSnapshot.ToString()), catalog, "123456", now, cache.Stamp(catalog, "123456"));
            second.RefreshAccounts();
            Require(second.Capture().SnapshotSha256 == PcrClanGuide.Hash(originalSnapshot) && !second.HasCurrentConfirmation, "list_reload_does_not_adopt_new_snapshot");
            var secondBoxes = (Dictionary<string, TextBox>)typeof(PcrClanGuideView).GetField("fields", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(second)!;
            secondBoxes["boss_name"].Text = "刷新期间未保存的 BOSS 备注"; beforeRefreshJob = second.Capture().Job.ToString();
            second.UseLatestAccount();
            Require(second.Capture().SnapshotSha256 == PcrClanGuide.Stamp(snapshotPath) && second.Capture().ConfirmedFor == null
                && second.Capture().Job.ToString() == beforeRefreshJob && File.ReadAllBytes(statePath).SequenceEqual(savedGuideBytes), "explicit_latest_account_preserves_draft_and_requires_review");
            second.ConfirmAndSave(); Require(second.HasCurrentConfirmation, "latest_account_can_be_reviewed_again");
        }
        finally { File.WriteAllBytes(snapshotPath, originalSnapshot); File.WriteAllBytes(statePath, savedGuideBytes); second.Reload(); }
        second.LoadBundle(bundlePath); second.SelectJob("candidate");
        Require(second.Capture().Job["unit_ids"]!.Values<int>().SequenceEqual(ids) && second.Capture().Job["set_by_unit"]![ids[0].ToString()]!.Type == JTokenType.Boolean
            && !second.HasCurrentConfirmation, "ui_prefills_candidates_but_never_confirmation");
        second.SelectJob("unknown-axis"); Require(second.Capture().Job["switches"]!.Type == JTokenType.Null, "unknown_axis_not_silently_empty");
        second.SaveDraft(); var unknownReopen = new PcrClanGuideView(root, statePath, config);
        Require(unknownReopen.Capture().Job["switches"]!.Type == JTokenType.Null && !unknownReopen.HasCurrentConfirmation, "unknown_axis_survives_draft_reopen");
        Throws(() => unknownReopen.ConfirmAndSave(), "unknown_axis_cannot_confirm_after_only_opening_review");
        var axisBox = (TextBox)typeof(PcrClanGuideView).GetField("switches", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(unknownReopen)!;
        axisBox.Text = "无切换"; unknownReopen.ConfirmAndSave();
        Require(unknownReopen.HasCurrentConfirmation && unknownReopen.Capture().Job["switches"] is JArray emptyAxis && emptyAxis.Count == 0, "explicit_no_switches_resolves_unknown_axis");
        var selectionBundle = (JObject)bundle.DeepClone();
        ((JArray)selectionBundle["pages"]!).Add(new JObject { ["page"] = 3, ["cid"] = 42155640155L, ["label"] = "无识别结果" });
        ((JArray)selectionBundle["jobs"]!).Add(Job("bc-a2", 1, "套餐甲"));
        var selectionPath = Path.Combine(root, "selection-bundle.json"); File.WriteAllText(selectionPath, selectionBundle.ToString(), new UTF8Encoding(false));
        var selectionView = new PcrClanGuideView(root, statePath, config);
        var selection = (Expander)typeof(PcrClanGuideView).GetField("workSelection", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(selectionView)!;
        var nearStatusField = typeof(PcrClanGuideView).GetField("videoStatus", BindingFlags.Instance | BindingFlags.NonPublic);
        Require(nearStatusField != null, "fetch_feedback_control_near_button");
        var nearStatus = (TextBlock)nearStatusField!.GetValue(selectionView)!;
        var videoInput = (TextBox)typeof(PcrClanGuideView).GetField("video", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(selectionView)!;
        var fetch = typeof(PcrClanGuideView).GetMethod("FetchVideo", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var originalState = File.ReadAllBytes(statePath);
        selection.IsExpanded = true; videoInput.Text = "https://example.com/video/BV1hQnb6XEyf/";
        ((System.Threading.Tasks.Task)fetch.Invoke(selectionView, null)!).GetAwaiter().GetResult();
        Require(nearStatus.Text!.Contains("B 站 HTTPS") && ((StackPanel)selection.Content!).Children.Contains(nearStatus)
            && File.ReadAllBytes(statePath).SequenceEqual(originalState), "fetch_invalid_url_feedback_near_button_preserves_saved");

        var pythonDir = Path.Combine(root, ".venv", "Scripts"); Directory.CreateDirectory(pythonDir);
        File.Copy(python, Path.Combine(pythonDir, "python.exe"));
        var sourcePythonDir = Path.GetDirectoryName(Path.GetFullPath(python))!;
        var originalVenv = Path.GetFullPath(Path.Combine(sourcePythonDir, "..", "pyvenv.cfg"));
        File.WriteAllText(Path.Combine(root, ".venv", "pyvenv.cfg"), File.Exists(originalVenv) ? File.ReadAllText(originalVenv) : "home = " + sourcePythonDir + "\ninclude-system-site-packages = false\n", new UTF8Encoding(false));
        var pythonHome = File.Exists(originalVenv) ? File.ReadLines(originalVenv).FirstOrDefault(line => line.StartsWith("home = ", StringComparison.Ordinal))?[7..].Trim() ?? sourcePythonDir : sourcePythonDir;
        foreach (var dll in Directory.EnumerateFiles(pythonHome, "python3*.dll")) File.Copy(dll, Path.Combine(pythonDir, Path.GetFileName(dll)));
        var p2Bundle = (JObject)bundle.DeepClone();
        p2Bundle["pages"] = new JArray(((JArray)bundle["pages"]!).Where(p => (int?)p["page"] == 2).Select(p => p.DeepClone()));
        p2Bundle["jobs"] = new JArray(((JArray)bundle["jobs"]!).Where(j => (int?)j["source"]?["page"] == 2).Select(j => j.DeepClone()));
        var p2Path = Path.Combine(root, "p2-bundle.json"); File.WriteAllText(p2Path, p2Bundle.ToString(), new UTF8Encoding(false));
        var scripts = Path.Combine(root, "scripts"); Directory.CreateDirectory(scripts);
        var stub = "import json, pathlib, shutil, sys, time\n" +
            "out = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n" +
            "url = sys.argv[sys.argv.index('--video-url') + 1]\n" +
            "time.sleep(0.4)\n" +
            "if 'p=3' in url or 'offline412' in url:\n    print('urllib.error.HTTPError: HTTP Error 412: Precondition Failed', file=sys.stderr)\n    sys.exit(2)\n" +
            "if 'p=2' not in url:\n    print('ValueError: 合成获取失败', file=sys.stderr)\n    sys.exit(2)\n" +
            "out.mkdir(parents=True)\n" +
            "shutil.copyfile(" + JsonConvert.SerializeObject(p2Path) + ", out / 'bundle.json')\n" +
            "print(json.dumps({'bundle_path': str((out / 'bundle.json').resolve())}))\n";
        File.WriteAllText(Path.Combine(scripts, "prepare-clan-guide.py"), stub, new UTF8Encoding(false));
        void CompleteFetch(System.Threading.Tasks.Task task)
        {
            var clock = System.Diagnostics.Stopwatch.StartNew();
            while (!task.IsCompleted && clock.Elapsed < TimeSpan.FromSeconds(20)) { Dispatcher.UIThread.RunJobs(); System.Threading.Thread.Sleep(10); }
            Require(task.IsCompleted, "fetch_fixture_finishes"); task.GetAwaiter().GetResult();
        }
        // The acquisition and recognition are synthetic and stay inside the fixture directory.
        var mediaRoot = Path.Combine(root, ".runtime", "material");
        var mediaPage = Path.Combine(mediaRoot, "p2");
        var mediaFrames = Path.Combine(mediaPage, "frames"); Directory.CreateDirectory(mediaFrames);
        var mediaImage = Path.Combine(mediaFrames, "00012.png"); File.WriteAllBytes(mediaImage, originalImage);
        var cachedBundle = (JObject)selectionBundle.DeepClone();
        ((JArray)cachedBundle["jobs"]!).Add(Job("d-b", 2, "套餐乙"));
        ((JArray)cachedBundle["jobs"]!).Add(Job("d-c", 2, "套餐甲"));
        var cachedJobs = ((JArray)cachedBundle["jobs"]!).OfType<JObject>().Where(j => (int?)j["source"]?["page"] == 2).ToArray();
        foreach (var job in cachedJobs) { job["source"]!["image_path"] = mediaImage; job["source"]!["image_sha256"] = PcrClanGuide.Hash(originalImage); }
        var acquisition = new JObject { ["bvid"] = "BV1Rbh86XEsq", ["page"] = 2, ["cid"] = 42155640154L, ["retrieved_at"] = "2026-10-25T13:17:01+08:00" };
        var records = new JArray(new JObject { ["page"] = 2, ["acquisition"] = acquisition.DeepClone(), ["observations"] = new JArray(new JObject {
            ["at_seconds"] = 12, ["image_path"] = mediaImage, ["tables"] = new JArray(cachedJobs.Select(j => new JObject { ["job_id"] = j["source"]!["job_id"]!.DeepClone() })) }) });
        var localCache = Path.Combine(root, ".runtime", "clan-guide-sources", "synthetic-cache"); Directory.CreateDirectory(localCache);
        var cacheBundlePath = Path.Combine(localCache, "bundle.json");
        var cacheRecognitionPath = Path.Combine(localCache, "recognition.json");
        var sourceRecognitionPath = Path.Combine(mediaRoot, "recognition.json");
        var acquisitionPath = Path.Combine(mediaPage, "acquisition.json");
        void WriteRecords() { File.WriteAllText(cacheRecognitionPath, records.ToString(), new UTF8Encoding(false)); File.WriteAllText(sourceRecognitionPath, records.ToString(), new UTF8Encoding(false)); }
        File.WriteAllText(cacheBundlePath, cachedBundle.ToString(), new UTF8Encoding(false));
        File.WriteAllText(acquisitionPath, acquisition.ToString(), new UTF8Encoding(false)); WriteRecords();
        var findCache = typeof(PcrClanGuideView).GetMethod("FindCachedVideo", BindingFlags.Instance | BindingFlags.NonPublic)!;
        bool Finds(string url) => findCache.Invoke(selectionView, new object[] { new Uri(url), System.Threading.CancellationToken.None }) != null;
        var cacheUrl = "https://www.bilibili.com/video/BV1Rbh86XEsq/?p=2&vd_source=offline412";
        Require(Finds(cacheUrl), "cached_video_matches_bv_page_cid_and_evidence");
        Require(!Finds("https://www.bilibili.com/video/BV1hQnb6XEyf/?p=2")
            && !Finds("https://www.bilibili.com/video/BV1Rbh86XEsq/?p=3")
            && !Finds("https://www.bilibili.com/video/BV1Rbh86XEsq/")
            && !Finds("https://www.bilibili.com/video/BV1Rbh86XEsq/?p=2&p=2"), "cached_video_requires_exact_bv_and_one_explicit_page");
        File.WriteAllBytes(mediaImage, originalImage.Concat(new byte[] { 1 }).ToArray());
        Require(!Finds(cacheUrl), "cached_video_rejects_changed_image"); File.WriteAllBytes(mediaImage, originalImage);
        acquisition["cid"] = 1; File.WriteAllText(acquisitionPath, acquisition.ToString(), new UTF8Encoding(false));
        Require(!Finds(cacheUrl), "cached_video_rejects_changed_acquisition"); acquisition["cid"] = 42155640154L; File.WriteAllText(acquisitionPath, acquisition.ToString(), new UTF8Encoding(false));
        records[0]!["acquisition"]!["retrieved_at"] = JValue.CreateNull(); WriteRecords();
        Require(!Finds(cacheUrl), "cached_video_rejects_unknown_retrieval_time"); records[0]!["acquisition"]!["retrieved_at"] = "2026-10-25T13:17:01+08:00"; WriteRecords();
        var cacheNotice = (TextBlock)typeof(PcrClanGuideView).GetField("cacheNotice", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(selectionView)!;
        var oldContext = System.Threading.SynchronizationContext.Current;
        System.Threading.SynchronizationContext.SetSynchronizationContext(new AvaloniaSynchronizationContext(Dispatcher.UIThread, DispatcherPriority.Normal));
        try
        {
        videoInput.Text = "https://www.bilibili.com/video/BV1hQnb6XEyf/?p=1";
        var failedFetch = (System.Threading.Tasks.Task)fetch.Invoke(selectionView, null)!;
        Require(!failedFetch.IsCompleted && nearStatus.Text!.Contains("正在获取"), "fetch_progress_visible_near_button");
        CompleteFetch(failedFetch);
        Require(nearStatus.Text!.Contains("合成获取失败") && !nearStatus.Text.Contains("Traceback")
            && File.ReadAllBytes(statePath).SequenceEqual(originalState), "fetch_child_failure_reason_preserves_saved");
        videoInput.Text = "https://www.bilibili.com/video/BV1hQnb6XEyf/?p=3";
        CompleteFetch((System.Threading.Tasks.Task)fetch.Invoke(selectionView, null)!);
        Require(nearStatus.Text!.Contains("HTTP 412") && nearStatus.Text.Contains("重试") && nearStatus.Text.Contains("导入")
            && File.ReadAllBytes(statePath).SequenceEqual(originalState), "fetch_http_412_has_actionable_feedback");
        videoInput.Text = cacheUrl;
        CompleteFetch((System.Threading.Tasks.Task)fetch.Invoke(selectionView, null)!);
        Require(selection.IsExpanded && nearStatus.Text!.Contains("本地缓存") && cacheNotice.Text!.Contains("2026-10-25")
            && cacheNotice.Text.Contains("线上是否更新尚未核验") && !selectionView.HasCurrentConfirmation
            && File.ReadAllBytes(statePath).SequenceEqual(originalState), "fetch_412_uses_matching_cache_as_unconfirmed_preview");
        var cachedPage = (ComboBox)typeof(PcrClanGuideView).GetField("page", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(selectionView)!;
        var cachedPackage = (ComboBox)typeof(PcrClanGuideView).GetField("package", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(selectionView)!;
        var cachedTeam = (ComboBox)typeof(PcrClanGuideView).GetField("teams", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(selectionView)!;
        cachedPackage.SelectedIndex = 1; Dispatcher.UIThread.RunJobs(); CompleteLoading(selectionView);
        cachedPackage.SelectedIndex = 0; cachedTeam.SelectedIndex = 1; Dispatcher.UIThread.RunJobs(); CompleteLoading(selectionView);
        cachedPage.SelectedIndex = 0; cachedPage.SelectedIndex = 1; Dispatcher.UIThread.RunJobs(); CompleteLoading(selectionView);
        Require(cacheNotice.Text!.Contains("本地缓存") && cacheNotice.Text.Contains("2026-10-25")
            && cacheNotice.Text.Contains("线上是否更新尚未核验") && !selectionView.HasCurrentConfirmation
            && File.ReadAllBytes(statePath).SequenceEqual(originalState), "cached_notice_persists_across_page_package_and_team_changes");
        videoInput.Text = "https://bilibili.com/video/BV1hQnb6XEyf/?vd_source=example&spm_id_from=example&p=2";
        var successfulFetch = (System.Threading.Tasks.Task)fetch.Invoke(selectionView, null)!; CompleteFetch(successfulFetch);
        Require(selection.IsExpanded && nearStatus.Text!.Contains("已载入") && string.IsNullOrEmpty(cacheNotice.Text)
            && (string?)selectionView.Capture().Job["phase"] == "D" && File.ReadAllBytes(statePath).SequenceEqual(originalState), "fetch_success_shows_p2_and_keeps_selection_open");

        }
        finally { System.Threading.SynchronizationContext.SetSynchronizationContext(oldContext); }

        selectionView.LoadBundle(selectionPath);
        var selectedPage = (ComboBox)typeof(PcrClanGuideView).GetField("page", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(selectionView)!;
        var selectedPackage = (ComboBox)typeof(PcrClanGuideView).GetField("package", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(selectionView)!;
        var selectedTeam = (ComboBox)typeof(PcrClanGuideView).GetField("teams", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(selectionView)!;
        Require(selection.IsExpanded && nearStatus.Text!.Contains("已载入"), "loaded_video_selection_remains_visible");
        selectedPage.SelectedIndex = 1; Dispatcher.UIThread.RunJobs(); CompleteLoading(selectionView);
        Require(selection.IsExpanded && (string?)selectionView.Capture().Job["phase"] == "D", "changing_page_keeps_selection_open");
        selectedPage.SelectedIndex = 0; selectedPackage.SelectedIndex = 1; Dispatcher.UIThread.RunJobs(); CompleteLoading(selectionView);
        Require(selection.IsExpanded && (string?)selectionView.Capture().Job["package"] == "套餐乙", "changing_package_keeps_selection_open");
        selectedPackage.SelectedIndex = 0; selectedTeam.SelectedIndex = 1; Dispatcher.UIThread.RunJobs(); CompleteLoading(selectionView);
        Require(selection.IsExpanded && selectionView.Capture().SelectedJobId == "bc-a2", "changing_team_keeps_selection_open");
        selection.IsExpanded = false; selectionView.SelectJob("d-a");
        Require(!selection.IsExpanded, "programmatic_selection_preserves_collapsed_state");
        selection.IsExpanded = true; selectedPage.SelectedIndex = 2; Dispatcher.UIThread.RunJobs(); CompleteLoading(selectionView);
        Require(selection.IsExpanded && nearStatus.Text!.Contains("未识别") && !selectionView.HasCurrentConfirmation
            && File.ReadAllBytes(statePath).SequenceEqual(originalState), "empty_page_is_visible_and_does_not_confirm_old_job");
        selectedPage.SelectedIndex = 1; Dispatcher.UIThread.RunJobs(); CompleteLoading(selectionView);
        Require(selection.IsExpanded && !nearStatus.Text!.Contains("未识别") && (string?)selectionView.Capture().Job["phase"] == "D", "valid_page_clears_empty_page_message");

        second.LoadBundle(bundlePath);
        var failurePagePicker = (ComboBox)typeof(PcrClanGuideView).GetField("page", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(second)!;
        var sourceDetails = (Expander)typeof(PcrClanGuideView).GetField("sourceDetails", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(second)!;
        sourceDetails.IsExpanded = true;
        File.WriteAllBytes(imagePath, originalImage.Concat(new byte[] { 0 }).ToArray());
        try
        {
            failurePagePicker.SelectedIndex = 1; Dispatcher.UIThread.RunJobs(); CompleteLoading(second);
            Require(second.StatusText.Contains("作业不可用") && !second.HasCurrentConfirmation, "selection_image_failure_stays_inside_ui");
        }
        finally { File.WriteAllBytes(imagePath, originalImage); }
        second.LoadBundle(bundlePath); second.SelectJob("d-a");
        Require(!second.HasCurrentConfirmation && (string?)second.Capture().Job["phase"] == "D", "ui_cross_page_never_reuses_confirmation");
        var pagePicker = (ComboBox)typeof(PcrClanGuideView).GetField("page", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(second)!;
        Require(pagePicker.SelectedItem!.ToString()!.Contains("P2"), "ui_programmatic_selection_keeps_page_picker_in_sync");
        sourceDetails.IsExpanded = false;
        second.Background = Avalonia.Media.Brushes.White;
        var host = new Window { Content = second, Background = Avalonia.Media.Brushes.White, Width = 1120, Height = 900, ShowInTaskbar = false, ShowActivated = false, Opacity = 0 };
        host.Show();
        try
        {
            second.Measure(new Size(1120, 900)); second.Arrange(new Rect(0, 0, 1120, 900)); second.UpdateLayout(); Dispatcher.UIThread.RunJobs();
            using var rendered = new RenderTargetBitmap(new PixelSize(1120, 900), new Vector(96, 96)); rendered.Render(second); rendered.Save(Path.Combine(root, "clan-guide-view.png"));
            Require((string?)second.Capture().Job["phase"] == "D", "offline_layout_keeps_selection");
        }
        finally { host.Close(); }
        Require(File.ReadAllBytes(legacy).SequenceEqual(originalLegacy) && File.ReadAllBytes(snapshotPath).SequenceEqual(originalSnapshot), "ui_keeps_shared_private_files");
        var displayOnlyRoot = Path.Combine(root, "display-roster-only"); var displayOnlyConfig = Path.Combine(displayOnlyRoot, "config");
        var sharedRoster = Path.Combine(displayOnlyRoot, "assets", "labyrinth", "character-pack", "characters.landosolroster.json"); Directory.CreateDirectory(Path.GetDirectoryName(sharedRoster)!);
        File.WriteAllText(sharedRoster, new JObject { ["schemaVersion"] = 1, ["characters"] = new JArray(catalog.Units.Select(u => new JObject { ["id"] = (u.UnitId / 100).ToString(), ["name"] = u.Name, ["officialName"] = u.Name })) }.ToString(), new UTF8Encoding(false));
        var freshView = new PcrClanGuideView(displayOnlyRoot, Path.Combine(displayOnlyConfig, "agent-guide-review.json"), displayOnlyConfig); freshView.LoadBundle(bundlePath);
        Require(freshView.Capture().Job["unit_ids"]!.Values<int>().SequenceEqual(ids) && !freshView.HasCurrentConfirmation, "fresh_profile_shares_roster_for_display_only");
        Throws(() => PcrClanGuide.Catalog(displayOnlyConfig, displayOnlyRoot, catalog.ContentVersion), "display_roster_never_forges_versioned_catalog");
        Throws(() => freshView.ConfirmAndSave(), "fresh_without_original_account_catalog_cannot_confirm");
        var supportRequirement = typeof(PcrClanGuide).GetMethod("SupportRequirement", BindingFlags.Static | BindingFlags.NonPublic)!;
        var ownQsjs = Copy(reopened); ownQsjs.Job["support_mode"] = "qsjs"; var ownOriginal = ownQsjs.Job.ToString();
        Require(supportRequirement.Invoke(null, new object[] { ownQsjs.Job, snapshot }) == null && ownQsjs.Job.ToString() == ownOriginal,
            "qsjs_owned_resolves_without_rewriting_source");
        var ownedSpecified = Copy(ownQsjs); ownedSpecified.Job["support_mode"] = "specified"; ownedSpecified.Job["support_unit_id"] = ids[0];
        Require((int)supportRequirement.Invoke(null, new object[] { ownedSpecified.Job, snapshot })! == ids[0], "specified_owned_still_requires_borrowed_support");
        var unknownOwnership = (JObject)snapshot.DeepClone(); unknownOwnership["unknown_fields"] = new JArray("units." + ids[0] + ".ownership");
        Throws(() => supportRequirement.Invoke(null, new object[] { ownQsjs.Job, unknownOwnership }), "qsjs_unknown_owned_stays_blocked");
        var fourStarSnapshot = (JObject)snapshot.DeepClone(); fourStarSnapshot["units"]![4]!["battle_rarity"] = 4;
        var fourStarBytes = Encoding.UTF8.GetBytes(fourStarSnapshot.ToString()); File.WriteAllBytes(snapshotPath, fourStarBytes);
        var fourStarJob = Copy(ownQsjs); fourStarJob.SnapshotSha256 = PcrClanGuide.Hash(fourStarBytes);
        var fourStarPlan = PcrClanGuide.BuildPlan(fourStarJob, config, root, now);
        Require((int)fourStarPlan["stars"]![4]! == 4 && fourStarJob.Job["stars"]![4]!.Type == JTokenType.Null, "qsjs_retains_account_four_star_without_rewriting_job");
        File.WriteAllBytes(snapshotPath, originalSnapshot);
        var asyncView = new PcrClanGuideView(root, statePath, config); asyncView.LoadBundle(bundlePath);
        var beforeAsync = File.ReadAllBytes(statePath);
        _ = asyncView.SelectJobAsync("bc-b"); Require(!asyncView.HasCurrentConfirmation, "pending_load_cannot_start");
        _ = asyncView.SelectJobAsync("d-a"); CompleteLoading(asyncView);
        Require(asyncView.Capture().SelectedJobId == "d-a" && File.ReadAllBytes(statePath).SequenceEqual(beforeAsync), "last_async_selection_wins_without_file_write");
        var asyncFields = (Dictionary<string, TextBox>)typeof(PcrClanGuideView).GetField("fields", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(asyncView)!;
        _ = asyncView.SelectJobAsync("bc-b"); asyncFields["boss_name"].Text = "载入期间保留编辑"; CompleteLoading(asyncView);
        Require(asyncView.Capture().SelectedJobId == "d-a" && asyncFields["boss_name"].Text == "载入期间保留编辑" && !asyncView.HasCurrentConfirmation,
            "edit_invalidates_pending_selection_without_losing_draft");
        var accountCase = new PcrClanGuideView(root, statePath, config); accountCase.LoadBundle(bundlePath); accountCase.SelectJob("d-a");
        var accountRows = (Array)typeof(PcrClanGuideView).GetField("members", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(accountCase)!;
        var manualSets = new[] { false, true, true, true, true };
        for (var index = 0; index < accountRows.Length; index++)
        {
            var row = accountRows.GetValue(index)!;
            ((ComboBox)row.GetType().GetProperty("Set")!.GetValue(row)!).SelectedIndex = manualSets[index] ? 1 : 2;
            if (index == 4) ((TextBox)row.GetType().GetProperty("Star")!.GetValue(row)!).Text = "5";
        }
        var manualJob = accountCase.Capture().Job.DeepClone(); var beforeAccountCaseState = File.ReadAllBytes(statePath);
        var beforeAccountCaseCache = File.ReadAllBytes(snapshotPath);
        var accountVideoStatus = (TextBlock)typeof(PcrClanGuideView).GetField("videoStatus", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(accountCase)!;
        void CompleteAccountCase(System.Threading.Tasks.Task operation)
        {
            var clock = System.Diagnostics.Stopwatch.StartNew();
            while (!operation.IsCompleted && clock.Elapsed < TimeSpan.FromSeconds(20)) { Dispatcher.UIThread.RunJobs(); System.Threading.Thread.Sleep(5); }
            Require(operation.IsCompleted, "async_account_case_finishes"); operation.GetAwaiter().GetResult(); CompleteLoading(accountCase);
        }
        try
        {
            var freshAccount = (JObject)snapshot.DeepClone(); freshAccount["observed_at"] = now.ToString("O"); freshAccount["units"]![4]!["battle_rarity"] = 5;
            cache.Save(Encoding.UTF8.GetBytes(freshAccount.ToString()), catalog, "123456", now, cache.Stamp(catalog, "123456"));
            var actualHash = PcrClanGuide.Stamp(snapshotPath)!;
            var wrongIdentity = new PcrGuideSnapshot("654321", "cn-bilibili", catalog.ContentVersion, now.ToString("O"), actualHash, Path.GetFileName(snapshotPath));
            var rejectedAccount = accountCase.UseAccountSnapshotAsync(wrongIdentity); CompleteAccountCase(rejectedAccount);
            Require(!rejectedAccount.GetAwaiter().GetResult() && accountVideoStatus.Text!.Contains("作业不可用") && accountVideoStatus.Text.Contains("身份"),
                "async_account_bad_identity_sets_visible_failure");
            var uppercaseAccount = new PcrGuideSnapshot("123456", "cn-bilibili", catalog.ContentVersion, now.ToString("O"), actualHash.ToUpperInvariant(), Path.GetFileName(snapshotPath));
            var acceptedAccount = accountCase.UseAccountSnapshotAsync(uppercaseAccount); CompleteAccountCase(acceptedAccount);
            var acceptedDocument = accountCase.Capture();
            Require(acceptedAccount.GetAwaiter().GetResult() && acceptedDocument.SnapshotSha256 == actualHash && acceptedDocument.SnapshotPath == snapshotPath,
                "async_account_uppercase_hash_uses_actual_lowercase_source_stamp");
            var acceptedPlan = PcrClanGuide.BuildPlan(acceptedDocument, config, root, DateTimeOffset.Now);
            Require((int)acceptedPlan["stars"]![4]! == 5 && acceptedPlan["unit_ids"]!.Values<int>().SequenceEqual(ids)
                && ids.Select(id => acceptedPlan["set_by_unit"]![id.ToString()]!.Value<bool>()).SequenceEqual(manualSets)
                && JToken.DeepEquals(manualJob, acceptedDocument.Job), "async_account_refresh_preserves_manual_fifth_star_and_all_sets");
            var afterAccountVideoStatus = accountVideoStatus.Text ?? "";
            Require(!afterAccountVideoStatus.Contains("作业不可用") && !afterAccountVideoStatus.Contains("身份不一致")
                && !accountCase.StatusText.Contains("作业不可用"), "async_account_success_clears_previous_video_error");
            Require(File.ReadAllBytes(statePath).SequenceEqual(beforeAccountCaseState) && !accountCase.HasCurrentConfirmation,
                "async_account_refresh_does_not_save_or_reconfirm_manual_draft");
        }
        finally { File.WriteAllBytes(snapshotPath, beforeAccountCaseCache); File.WriteAllBytes(statePath, beforeAccountCaseState); }
        var plans = new JArray(plan, PcrClanGuide.BuildPlan(cross, config, root, now), qsjsPlan, specifiedPlan, timedPlan);
        File.WriteAllText(Path.Combine(root, "managed-plan-cases.json"), plans.ToString(), new UTF8Encoding(false));
        File.WriteAllText(Path.Combine(root, "checks.json"), new JObject { ["checks"] = count, ["synthetic"] = true, ["game_input"] = false, ["original_cache_unchanged"] = true }.ToString(), new UTF8Encoding(false));
        Console.WriteLine($"PCR clan guide checks passed: {count}; synthetic controls, no game input");
    }
}
