using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using Avalonia.Media.Imaging;
using CommunityToolkit.Mvvm.ComponentModel;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Pcr;

public sealed class PcrLabyrinthRatingRow : ObservableObject, IDisposable {
    private bool iconLoaded;
    private Bitmap? icon;
    private string personalScore = "未设置（沿用原评分）";
    private bool isModified;
    public PcrLabyrinthRatingRow(string id, string name, string detail, double? originalScore,
        string searchText, string? iconPath = null) {
        Id=id; Name=name; Detail=detail; OriginalScore=originalScore; SearchText=Normalize(searchText); IconPath=iconPath;
    }
    public string Id { get; }
    public string Name { get; }
    public string Detail { get; }
    public double? OriginalScore { get; }
    public string OriginalScoreText => OriginalScore?.ToString("0.##", CultureInfo.InvariantCulture) ?? "暂无";
    public string SearchText { get; }
    public string? IconPath { get; }
    public string PersonalScore { get => personalScore; private set => SetProperty(ref personalScore,value); }
    public bool IsModified { get => isModified; private set => SetProperty(ref isModified,value); }
    public Bitmap? Icon {
        get {
            if (!iconLoaded) {
                iconLoaded=true;
                if (IconPath is not null) try { icon=new Bitmap(IconPath); } catch { icon=null; }
            }
            return icon;
        }
    }
    internal void Refresh(JObject scores) {
        IsModified=scores.TryGetValue(Id,out var value);
        PersonalScore=IsModified ? value!.ToString(Newtonsoft.Json.Formatting.None) : "未设置（沿用原评分）";
    }
    internal static string Normalize(string value) => new(value.Replace('（','(').Replace('）',')')
        .ToLowerInvariant().Where(ch=>!char.IsWhiteSpace(ch)).ToArray());
    public void Dispose() { icon?.Dispose(); icon=null; }
}

// The draft owns a copy: opening, filtering, or cancelling never normalizes legacy scores.
public sealed class PcrLabyrinthRatingsDraft : ObservableObject, IDisposable {
    private readonly JObject scores;
    private readonly IReadOnlyList<PcrLabyrinthRatingRow> rows;
    private string searchQuery = "";
    private bool modifiedOnly;
    private PcrLabyrinthRatingRow? selected;
    private string scoreInput = "";
    private string errorMessage = "";
    public PcrLabyrinthRatingsDraft(JObject scores, IEnumerable<PcrLabyrinthRatingRow> catalog) {
        this.scores=(JObject)scores.DeepClone();
        var items=catalog.ToDictionary(row=>row.Id,StringComparer.Ordinal);
        foreach (var saved in scores.Properties())
            if (!items.ContainsKey(saved.Name)) items.Add(saved.Name,
                new PcrLabyrinthRatingRow(saved.Name,$"未收录角色 {saved.Name}","保存的个人评分",null,saved.Name));
        rows=items.Values.OrderBy(row=>row.Id,StringComparer.Ordinal).ToArray();
        Refresh();
    }
    public ObservableCollection<PcrLabyrinthRatingRow> VisibleRows { get; } = new();
    public string SearchQuery { get => searchQuery; set { if (SetProperty(ref searchQuery,value ?? "")) Filter(); } }
    public bool ModifiedOnly { get => modifiedOnly; set { if (SetProperty(ref modifiedOnly,value)) Filter(); } }
    public PcrLabyrinthRatingRow? Selected {
        get => selected;
        set {
            if (!SetProperty(ref selected,value)) return;
            ScoreInput=value is null ? "" : scores.TryGetValue(value.Id,out var saved)
                ? saved!.ToString(Newtonsoft.Json.Formatting.None)
                : value.OriginalScore is double original ? Math.Round(original,MidpointRounding.AwayFromZero).ToString(CultureInfo.InvariantCulture) : "";
            ErrorMessage="";
            OnPropertyChanged(nameof(HasSelection));
        }
    }
    public bool HasSelection => Selected is not null;
    public string ScoreInput { get => scoreInput; set => SetProperty(ref scoreInput,value ?? ""); }
    public string ErrorMessage { get => errorMessage; private set => SetProperty(ref errorMessage,value); }
    public string ResultCount => $"{VisibleRows.Count} 人";
    public bool ApplySelected() {
        if (Selected is null) return false;
        if (!int.TryParse(ScoreInput,NumberStyles.None,CultureInfo.InvariantCulture,out var value) || value<0 || value>100) {
            ErrorMessage="请输入 0–100 的整数。";
            return false;
        }
        scores[Selected.Id]=value;
        ErrorMessage="已应用到草稿；保存后用于选人和编组。";
        Refresh();
        return true;
    }
    public void RestoreSelected() {
        if (Selected is null) return;
        var restored=Selected;
        scores.Remove(restored.Id);
        ScoreInput=restored.OriginalScore is double original
            ? Math.Round(original,MidpointRounding.AwayFromZero).ToString(CultureInfo.InvariantCulture) : "";
        ErrorMessage="已恢复此角色原评分；保存后生效。";
        Refresh();
    }
    public void ResetAll() {
        scores.RemoveAll();
        var current=Selected;
        Selected=null;
        Refresh();
        if (current is not null && VisibleRows.Contains(current)) Selected=current;
        ErrorMessage="已清除草稿中的全部个人评分；保存后生效。";
    }
    public JObject Snapshot() => (JObject)scores.DeepClone();
    private void Refresh() { foreach (var row in rows) row.Refresh(scores); Filter(); }
    private void Filter() {
        var needle=PcrLabyrinthRatingRow.Normalize(SearchQuery);
        var visible=rows.Where(row=>(!ModifiedOnly || row.IsModified)
            && (needle.Length==0 || row.SearchText.Contains(needle,StringComparison.Ordinal))).ToArray();
        if (VisibleRows.SequenceEqual(visible)) return;
        var previous=Selected;
        VisibleRows.Clear();
        foreach (var row in visible) VisibleRows.Add(row);
        Selected=previous is not null && visible.Contains(previous) ? previous : null;
        OnPropertyChanged(nameof(ResultCount));
    }
    public void Dispose() { foreach (var row in rows) row.Dispose(); }

    public static IReadOnlyList<PcrLabyrinthRatingRow> LoadCatalog(string repositoryRoot) {
        var root=Path.GetFullPath(repositoryRoot);
        var pack=Path.Combine(root,"assets","labyrinth","character-pack");
        JObject Read(string path) => JObject.Parse(File.ReadAllText(path,Encoding.UTF8));
        var characters=Read(Path.Combine(pack,"characters.landosolroster.json"))["characters"] as JArray
            ?? throw new InvalidOperationException("角色图鉴缺少 characters。");
        var decisions=Read(Path.Combine(root,"assets","labyrinth","labyrinth-role-decision.json"))["characters"] as JArray
            ?? throw new InvalidOperationException("角色评分资料缺少 characters。");
        var facts=decisions.OfType<JObject>().Where(row=>row["characterId"]?.Type==JTokenType.String)
            .ToDictionary(row=>(string)row["characterId"]!,StringComparer.Ordinal);
        var iconRows=Read(Path.Combine(pack,"icons.json"))["characterIcons"] as JArray ?? new JArray();
        var icons=iconRows.OfType<JObject>().Where(row=>row["ownerId"]?.Type==JTokenType.String && row["file"]?.Type==JTokenType.String)
            .GroupBy(row=>(string)row["ownerId"]!).ToDictionary(group=>group.Key,
                group=>group.OrderBy(row=>(string?)row["format"]=="png" ? 0 : 1)
                    .ThenBy(row=>(string?)row["variant"] switch { "61"=>0,"31"=>1,"11"=>2,_=>3 })
                    .Select(row=>(string)row["file"]!).ToArray(),StringComparer.Ordinal);
        var result=new List<PcrLabyrinthRatingRow>();
        foreach (var character in characters.OfType<JObject>()) {
            var id=(string?)character["id"] ?? "";
            if (!Regex.IsMatch(id,@"^[1-9][0-9]{3,5}$")) continue;
            facts.TryGetValue(id,out var fact);
            var name=(string?)character["officialName"];
            if (string.IsNullOrWhiteSpace(name)) name=(string?)character["name"] ?? (string?)fact?["displayName"] ?? id;
            var detail=string.Join(" · ",new[] { (string?)fact?["roleClass"],(string?)fact?["damageType"] switch {
                "PHYSICAL"=>"物理","MAGIC"=>"魔法",_=>null },(string?)fact?["attribute"] switch {
                "FIRE"=>"火","WATER"=>"水","WIND"=>"风","LIGHT"=>"光","DARK"=>"暗",_=>null } }.Where(value=>value is not null));
            var original=fact?["userScore"]?.Type is JTokenType.Integer or JTokenType.Float ? (double?)fact["userScore"] : null;
            if (original is double score && (!double.IsFinite(score) || score<0 || score>100)) original=null;
            var path=PcrLabyrinthCharacter.IconPathFor(pack,icons,id);
            if (path is not null) {
                path=Path.GetFullPath(path);
                if (!path.StartsWith(pack+Path.DirectorySeparatorChar,StringComparison.OrdinalIgnoreCase)) path=null;
            }
            var aliases=character["aliases"] as JArray ?? new JArray();
            var search=$"{id} {name} {(string?)character["name"]} {(string?)fact?["displayName"]} {string.Join(' ',aliases.Values<string>())}";
            result.Add(new PcrLabyrinthRatingRow(id,name!,detail,original,search,path));
        }
        if (result.Count==0 || result.Select(row=>row.Id).Distinct(StringComparer.Ordinal).Count()!=result.Count)
            throw new InvalidOperationException("角色图鉴为空或存在重复 ID。");
        return result;
    }
}
