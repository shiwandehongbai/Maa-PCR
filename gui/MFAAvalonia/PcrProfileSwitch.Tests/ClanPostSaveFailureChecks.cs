using System;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using MFAAvalonia.Pcr;
using Newtonsoft.Json.Linq;

internal static class ClanPostSaveFailureChecks
{
    public static void Run(string sourceRoot, string directory)
    {
        ClanWorkflowChecks.Run(sourceRoot, directory);
        var root = Path.Combine(Path.GetFullPath(directory), "clan-workflow"); var config = Path.Combine(root, "config");
        var guide = Path.Combine(config, "agent-guide-review.json"); var original = PcrClanGuide.Read(guide); var originalBytes = File.ReadAllBytes(guide);
        var daily = PcrClanDailyPlan.Read(PcrClanDailyPlan.PathFor(config), out _);
        var target = PcrClanDailyPlan.Load(daily, 1, guide, config, sourceRoot, DateTimeOffset.Now, out _);
        var faultDirectory = Path.Combine(root, "final-readback-fault"); Directory.CreateDirectory(faultDirectory);
        var faultGuide = Path.Combine(faultDirectory, "agent-guide-review.json");
        var view = new PcrClanGuideView(sourceRoot, faultGuide, config); view.Display(original);
        var readSaved = typeof(PcrClanGuideView).GetProperty("ReadSavedGuide", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var originalReader = readSaved.GetValue(view);
        var count = 0; var readCount = 0; string? readPath = null; string? errorType = null;
        const string injectedFailure = "injected final guide readback IOException";
        void Check(bool value, string label) { if (!value) throw new InvalidOperationException("clan_postsave_" + label); count++; }
        void ReadbackFails(Action action, string label)
        {
            try { action(); }
            catch (Exception ex) when (ex.GetBaseException() is IOException failure && failure.Message == injectedFailure)
            { errorType = ex.GetBaseException().GetType().Name; count++; return; }
            throw new InvalidOperationException("clan_postsave_expected_readback_failure_" + label);
        }
        var before = view.Capture().SelectedJobId; var draft = view.Capture().Job.ToString();
        Check(view.HasCurrentConfirmation, "visible_old_selection_initially_confirmed");
        try
        {
            readSaved.SetValue(view, new Func<string, (PcrClanGuideDocument Document, string? Stamp)>(path =>
            {
                readCount++; readPath = path;
                var written = PcrClanGuide.Read(path, out var stamp);
                Check(path == faultGuide && stamp != null && written.SelectedJobId == target.SelectedJobId
                    && written.ConfirmedFor == target.ConfirmedFor && written.SavedJobs.Count == 3, "final_reader_observes_actual_saved_target");
                throw new IOException(injectedFailure);
            }));
            var load = typeof(PcrClanGuideView).GetMethod("LoadReviewed", BindingFlags.NonPublic | BindingFlags.Instance)!;
            ReadbackFails(() => load.Invoke(view, new object?[] { target, null }), "actual_loadreviewed_postsave_read");
            Check(File.Exists(faultGuide), "real_save_created_destination_before_read_failure");
            Check(readCount == 1 && readPath == faultGuide, "only_final_saved_guide_read_is_injected_once");
            Check(view.Capture().SelectedJobId == before && view.Capture().ConfirmedFor == null && !view.HasCurrentConfirmation
                && view.ExecutionConfirmationForStart == null, "old_visible_selection_cannot_authorize_start");
            Check(view.Capture().Job.ToString() == draft && File.ReadAllBytes(guide).SequenceEqual(originalBytes), "failed_readback_preserves_draft_and_source_bytes");
            Check(view.StatusText.Contains("旧显示已取消确认"), "visible_failure_explains_confirmation_revocation");
            var rejectedSave = false; try { view.ConfirmAndSave(); } catch (InvalidOperationException) { rejectedSave = true; }
            Check(rejectedSave, "unloaded_editor_cannot_overwrite_failed_destination");
        }
        finally { readSaved.SetValue(view, originalReader); }
        var readerRestored = ReferenceEquals(readSaved.GetValue(view), originalReader);
        Check(readerRestored, "final_saved_guide_reader_restored");
        var readBack = PcrClanGuide.Read(faultGuide);
        Check(readBack.SelectedJobId == target.SelectedJobId && readBack.ConfirmedFor == target.ConfirmedFor && readBack.SavedJobs.Count == 3, "actual_saved_target_preserved_without_blind_rollback");
        view.Reload(); Check(view.HasCurrentConfirmation && view.Capture().SelectedJobId == target.SelectedJobId, "explicit_reload_recovers_actual_saved_target");
        Check(PcrClanGuide.Read(guide).SelectedJobId == original.SelectedJobId && File.ReadAllBytes(guide).SequenceEqual(originalBytes), "source_guide_unchanged");
        File.WriteAllText(Path.Combine(root, "postsave-checks.json"), new JObject { ["checks"] = count, ["fault"] = injectedFailure,
            ["real_method"] = "PcrClanGuideView.LoadReviewed", ["stage"] = "final readback after saved target", ["error_type"] = errorType,
            ["read_count"] = readCount, ["read_path"] = readPath, ["reader_restored"] = readerRestored, ["acl_changed"] = false, ["game_input"] = false,
            ["source_guide"] = guide, ["fault_destination"] = faultGuide, ["saved_target"] = target.SelectedJobId }.ToString(), new UTF8Encoding(false));
        Console.WriteLine($"PCR clan post-save readback checks passed: {count}; actual save, injected IOException, confirmation revoked, no ACL change, game input=0");
    }
}
