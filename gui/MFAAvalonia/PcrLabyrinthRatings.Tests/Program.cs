using System;
using System.IO;
using System.Linq;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Interactivity;
using Avalonia.Media.Imaging;
using Avalonia.Styling;
using Avalonia.Threading;
using MFAAvalonia.Pcr;
using Newtonsoft.Json.Linq;

if (args.Length!=2 || args.Any(arg=>!Path.IsPathFullyQualified(arg))) {
    Console.Error.WriteLine("Rating checks require absolute evidence and source directories.");
    return 2;
}
try {
    var count=0;
    void Require(bool value,string name) { if (!value) throw new InvalidOperationException(name); count++; }
    var input=JObject.Parse("{\"1001\":73.5,\"999999\":62.25}");
    PcrLabyrinthRatingRow Row(string id,string name,double? score) => new(id,name,"测试角色",score,$"{id} {name}");
    using (var draft=new PcrLabyrinthRatingsDraft(input,new[] { Row("1001","日和莉（夏日）",46.7),Row("1002","优衣",80.0) })) {
        Require(JToken.DeepEquals(draft.Snapshot(),input),"opening_preserves_legacy_decimal_and_unknown_scores");
        var detached=draft.Snapshot(); detached["1001"]=1;
        Require((double)draft.Snapshot()["1001"]! ==73.5,"draft_snapshots_do_not_leak_mutable_state");
        draft.SearchQuery="夏日";
        Require(draft.VisibleRows.Count==1 && draft.VisibleRows[0].Id=="1001","search_version");
        draft.SearchQuery="日和莉(夏日)";
        Require(draft.VisibleRows.Count==1,"search_normalizes_parentheses");
        draft.SearchQuery="1002";
        Require(draft.VisibleRows.Single().Name=="优衣","search_id");
        draft.SearchQuery=""; draft.ModifiedOnly=true;
        Require(draft.VisibleRows.Select(row=>row.Id).SequenceEqual(new[] { "1001","999999" }),"modified_filter_keeps_unknown_roles");
        draft.Selected=draft.VisibleRows[0];
        Require(draft.ScoreInput=="73.5","selecting_legacy_decimal_does_not_round_it");
        foreach (var invalid in new[] { "","-1","101","10.5"," 80","abc","999999999999" }) {
            draft.ScoreInput=invalid;
            Require(!draft.ApplySelected() && (double)draft.Snapshot()["1001"]! ==73.5,"invalid_score_rejected_"+invalid);
        }
        draft.ScoreInput="0";
        Require(draft.ApplySelected() && (int)draft.Snapshot()["1001"]! ==0,"score_zero_is_allowed");
        draft.ScoreInput="100";
        Require(draft.ApplySelected() && (int)draft.Snapshot()["1001"]! ==100,"score_hundred_is_allowed");
        Require((double)draft.Snapshot()["999999"]! ==62.25 && JToken.DeepEquals(input,JObject.Parse("{\"1001\":73.5,\"999999\":62.25}")),
            "editing_one_role_preserves_other_scores_and_original_input");
        draft.RestoreSelected();
        Require(!draft.Snapshot().ContainsKey("1001") && draft.Snapshot().ContainsKey("999999"),"single_restore_only_removes_selected_override");
        draft.ResetAll();
        Require(draft.Snapshot().Count==0 && draft.VisibleRows.Count==0,"reset_all_includes_unknown_saved_overrides");
    }
    var catalog=PcrLabyrinthRatingsDraft.LoadCatalog(args[1]);
    Require(catalog.Count>=360 && catalog.Select(row=>row.Id).Distinct().Count()==catalog.Count,"catalog_has_all_roles_without_duplicate_ids");
    Require(catalog.Any(row=>row.OriginalScore is >0) && catalog.Any(row=>row.IconPath is not null && File.Exists(row.IconPath)),
        "catalog_uses_real_original_ratings_and_existing_portraits");
    foreach (var row in catalog) row.Dispose();
    AppBuilder.Configure<Application>().UsePlatformDetect().SetupWithoutStarting();
    Application.Current!.RequestedThemeVariant=ThemeVariant.Light;
    Application.Current.Styles.Add(new SukiUI.SukiTheme());
    Directory.CreateDirectory(args[0]);
    var applied=0; JObject? result=null;
    void Click(Window window,string name) {
        window.FindControl<Button>(name)!.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
        Dispatcher.UIThread.RunJobs();
    }
    PcrLabyrinthRatingsView Open(Action<JObject> save) {
        var window=new PcrLabyrinthRatingsView(input,save,args[1]) { ShowActivated=false,ShowInTaskbar=false,
            Position=new PixelPoint(-12000,-12000) };
        window.Show(); Dispatcher.UIThread.RunJobs(); window.UpdateLayout();
        return window;
    }
    var view=Open(value=>{ applied++; result=value; });
    try {
        Require(view.FindControl<TextBox>("RatingSearch") is not null && view.FindControl<ListBox>("RatingRows") is not null,
            "real_ratings_window_loads_search_and_virtualized_list");
        var draft=(PcrLabyrinthRatingsDraft)view.DataContext!;
        draft.SearchQuery="1001";
        draft.Selected=draft.VisibleRows.Single(row=>row.Id=="1001");
        Dispatcher.UIThread.RunJobs();
        Require(view.FindControl<Button>("ApplyRatingButton")!.IsEffectivelyEnabled
            && view.FindControl<Button>("RestoreRatingButton")!.IsEffectivelyEnabled,"selection_enables_real_editor_buttons");
        draft.ScoreInput="88"; Click(view,"ApplyRatingButton");
        Require((int)draft.Snapshot()["1001"]! ==88 && applied==0,"applying_row_does_not_save_before_explicit_save");
        Require(draft.HasSelection && view.FindControl<Button>("ApplyRatingButton")!.IsEffectivelyEnabled,
            "applying_a_score_keeps_selected_role_and_edit_controls");
        using var frame=new RenderTargetBitmap(new PixelSize(840,760));
        frame.Render(view); frame.Save(Path.Combine(args[0],"personal-ratings.png"));
        view.Width=620; view.Height=480;
        Dispatcher.UIThread.RunJobs(); view.UpdateLayout();
        using var compact=new RenderTargetBitmap(new PixelSize(620,480));
        compact.Render(view); compact.Save(Path.Combine(args[0],"personal-ratings-compact.png"));
        Click(view,"CancelRatingsButton");
        Require(applied==0 && (double)input["1001"]! ==73.5,"cancel_discards_window_draft");
    } finally { view.Close(); Dispatcher.UIThread.RunJobs(); }
    view=Open(value=>{ applied++; result=value; });
    try {
        var draft=(PcrLabyrinthRatingsDraft)view.DataContext!;
        draft.SearchQuery="1001"; draft.Selected=draft.VisibleRows.Single(row=>row.Id=="1001");
        draft.ScoreInput="89"; Click(view,"ApplyRatingButton"); Click(view,"SaveRatingsButton");
        Require(applied==1 && (int)result!["1001"]! ==89 && (double)result["999999"]! ==62.25,"save_keeps_untouched_unknown_decimal_score");
    } finally { view.Close(); Dispatcher.UIThread.RunJobs(); }
    view=Open(_=>throw new IOException("fixture save failure"));
    try {
        Click(view,"SaveRatingsButton");
        Require(view.IsVisible && view.FindControl<TextBlock>("RatingsSaveError")!.Text!.Contains("保存失败"),"save_failure_keeps_draft_open_and_visible");
    } finally { view.Close(); Dispatcher.UIThread.RunJobs(); }
    Console.WriteLine($"PASS {count} personal rating checks");
    return 0;
} catch (Exception error) { Console.Error.WriteLine(error); return 1; }
