using System;
using Avalonia.Controls;
using Avalonia.Interactivity;
using Avalonia.Markup.Xaml;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Pcr;

public partial class PcrLabyrinthRatingsView : Window {
    private readonly PcrLabyrinthRatingsDraft draft;
    private readonly Action<JObject> apply;
    public PcrLabyrinthRatingsView(JObject scores, Action<JObject> apply, string? repositoryRoot = null) {
        this.apply=apply;
        draft=new PcrLabyrinthRatingsDraft(scores,PcrLabyrinthRatingsDraft.LoadCatalog(
            repositoryRoot ?? PcrRuntimePaths.ResolveDataRepository(AppContext.BaseDirectory)));
        AvaloniaXamlLoader.Load(this);
        DataContext=draft;
        Closed+=(_,_)=>draft.Dispose();
    }
    private void ApplyRating(object? sender,RoutedEventArgs args) => draft.ApplySelected();
    private void RestoreRating(object? sender,RoutedEventArgs args) => draft.RestoreSelected();
    private void ResetRatings(object? sender,RoutedEventArgs args) => draft.ResetAll();
    private void CancelRatings(object? sender,RoutedEventArgs args) => Close(false);
    private void SaveRatings(object? sender,RoutedEventArgs args) {
        try { apply(draft.Snapshot()); Close(true); }
        catch (Exception) {
            // Keep the draft open if the account's settings could not be saved.
            this.FindControl<TextBlock>("RatingsSaveError")?.SetCurrentValue(TextBlock.TextProperty,"评分保存失败，请重试。");
        }
    }
}
