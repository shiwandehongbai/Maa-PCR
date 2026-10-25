using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using CommunityToolkit.Mvvm.ComponentModel;
using MFAAvalonia.Configuration;
using MFAAvalonia.Helper;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Pcr;

public sealed partial class PcrLabyrinthAccount : ObservableObject
{
    public string Id { get; }
    [ObservableProperty] private string name;
    internal string Server { get; set; }
    internal JObject Settings { get; set; }
    internal string Credentials { get; set; }
    internal PcrLabyrinthAccount(string id, string name, string server, JObject settings, string credentials)
    {
        Id=id; this.name=name; Server=server; Settings=(JObject)settings.DeepClone(); Credentials=credentials;
    }
    public override string ToString() => Name;
}

public sealed record PcrLabyrinthServer(string Id, string Name)
{
    public override string ToString() => Name;
    public static IReadOnlyList<PcrLabyrinthServer> All { get; } = new[] {
        new PcrLabyrinthServer("cn-bilibili", "国服 B 服"), new("cn-xiaomi", "小米服"),
        new("cn-huawei", "华为服"), new("cn-vivo", "vivo 服"), new("cn-oppo", "OPPO 服"),
        new("cn-aligames", "九游服"), new("cn-4399", "4399 服"), new("cn-mumu", "MuMu 服"),
        new("cn-tencent", "应用宝服") };
}

// The active legacy keys remain aliases so existing profiles and credential recovery keep working.
internal sealed class PcrLabyrinthAccounts
{
    internal const string Key="PcrLabyrinthAccountsV1";
    private readonly InstanceConfiguration configuration;
    private string expectedBook, expectedSettings, expectedCredentials;
    internal IReadOnlyList<PcrLabyrinthAccount> Accounts { get; }
    internal string SelectedId { get; }

    internal PcrLabyrinthAccounts(InstanceConfiguration configuration)
    {
        this.configuration=configuration;
        expectedBook=configuration.GetValue(Key, "");
        expectedSettings=configuration.GetValue(PcrLabyrinthSettings.Key, "");
        expectedCredentials=configuration.GetValue(PcrLabyrinthSettings.CredentialsKey, "");
        if (expectedBook.Length==0) {
            var first=new PcrLabyrinthAccount(Guid.NewGuid().ToString("N"), "账号 1", "cn-bilibili",
                expectedSettings.Length==0 ? new JObject() : JObject.Parse(expectedSettings), expectedCredentials);
            Accounts=new[] { first }; SelectedId=first.Id; return;
        }
        var root=JObject.Parse(expectedBook);
        if ((int?)root["version"]!=1 || root.Properties().Any(item=>!new[]{"version","selected_id","accounts"}.Contains(item.Name)) || root["accounts"] is not JArray rows || rows.Count is <1 or >100)
            throw new InvalidDataException("账号库格式无法读取，原内容已保留。");
        var accounts=rows.Select(row=> {
            if (row is not JObject value || value.Properties().Any(item=>!new[]{"id","name","server","settings","credentials"}.Contains(item.Name)) || value["settings"] is not JObject settings
                || value["credentials"]?.Type!=JTokenType.String) throw new InvalidDataException("账号库记录无法读取。");
            var id=(string?)value["id"] ?? ""; var name=(string?)value["name"] ?? ""; var server=(string?)value["server"] ?? "";
            if (!Guid.TryParseExact(id, "N", out _) || name.Length is <1 or >40 || name.Any(char.IsControl)
                || server.Length is <1 or >64 || server.Any(char.IsControl)) throw new InvalidDataException("账号库记录无效。");
            return new PcrLabyrinthAccount(id,name,server,settings,(string)value["credentials"]!);
        }).ToArray();
        SelectedId=(string?)root["selected_id"] ?? "";
        if (accounts.Select(account=>account.Id).Distinct().Count()!=accounts.Length
            || accounts.SingleOrDefault(account=>account.Id==SelectedId) is not { } selected)
            throw new InvalidDataException("账号库选择记录无效。");
        // Edits made by an older compatible client apply only to its selected account.
        selected.Settings=expectedSettings.Length==0 ? new JObject() : JObject.Parse(expectedSettings);
        selected.Credentials=expectedCredentials;
        Accounts=accounts;
    }

    internal void Save(IReadOnlyList<PcrLabyrinthAccount> accounts, string selectedId)
    {
        var selected=accounts.Single(account=>account.Id==selectedId);
        var book=new JObject { ["version"]=1,["selected_id"]=selectedId,["accounts"]=new JArray(accounts.Select(account=>new JObject {
            ["id"]=account.Id,["name"]=account.Name,["server"]=account.Server,
            ["settings"]=account.Settings.DeepClone(),["credentials"]=account.Credentials })) }.ToString(Formatting.None);
        var settings=selected.Settings.ToString(Formatting.None);
        var path=configuration.GetConfigFilePath(); Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        using var gate=new FileStream(path+".labyrinth.lock",FileMode.OpenOrCreate,FileAccess.ReadWrite,FileShare.None);
        var latest=File.Exists(path) ? JObject.Parse(File.ReadAllText(path,Encoding.UTF8)) : new JObject();
        string Value(string key) => (string?)latest[key] ?? "";
        if (Value(Key)!=expectedBook || Value(PcrLabyrinthSettings.Key)!=expectedSettings
            || Value(PcrLabyrinthSettings.CredentialsKey)!=expectedCredentials)
            throw new IOException("账号或设置已被其他窗口修改，本次没有覆盖；请重新打开该配置。");
        latest[Key]=book; latest[PcrLabyrinthSettings.Key]=settings;
        latest[PcrLabyrinthSettings.CredentialsKey]=selected.Credentials;
        JsonHelper.SaveJsonTextAtomicallyOrThrow(path,latest.ToString(Formatting.Indented));
        configuration.ReloadFromDisk();
        expectedBook=book; expectedSettings=settings; expectedCredentials=selected.Credentials;
    }
}
