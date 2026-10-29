using System;
using System.Threading;
using System.Threading.Tasks;
using MFAAvalonia.Configuration;
using MFAAvalonia.Helper;
using MFAAvalonia.ViewModels.Pages;

namespace MFAAvalonia.Pcr;

internal static class PcrStartupActions
{
    private static TaskQueueViewModel? launching;
    private static CancellationTokenSource? cancellation;
    private static Action? afterCancellation;

    internal static Task RunAsync(TaskQueueViewModel vm) =>
        RunAsync(vm, token => vm.Processor.StartSoftware(token));

    internal static async Task RunAsync(TaskQueueViewModel vm, Func<CancellationToken, Task> launchSoftware)
    {
        if (PcrFormalPreflight.SettingsOnly) return;
        if (GlobalConfiguration.GetValue(ConfigurationKeys.NoAutoStart, bool.FalseString) == bool.TrueString)
        {
            GlobalConfiguration.SetValue(ConfigurationKeys.NoAutoStart, bool.FalseString);
            return;
        }
        var action = vm.Processor.InstanceConfiguration.GetValue(ConfigurationKeys.BeforeTask, "None");
        if (action == "None") return;
        using var cancel = new CancellationTokenSource();
        try
        {
            if (action is not ("StartupSoftware" or "StartupSoftwareAndScript" or "StartupScriptOnly"))
                throw new InvalidOperationException("启动后操作无效，请重新选择并保存。");
            lock (PcrProfileRunGate.StartGate)
            {
                if (PcrProfileRunGate.Busy || !vm.CanEditPcrTarget) return;
                PcrRuntimePaths.RequireUpdateComplete(AppContext.BaseDirectory, PcrAlphaOwner.Repository);
                launching = vm;
                cancellation = cancel;
                vm.IsRunning = true;
                PcrProfileRunGate.Notify();
            }
            if (action != "StartupScriptOnly") await launchSoftware(cancel.Token);
            Task start;
            lock (PcrProfileRunGate.StartGate)
            {
                cancel.Token.ThrowIfCancellationRequested();
                launching = null;
                cancellation = null;
                vm.IsRunning = false;
                if (action == "StartupSoftware") return;
                // Automatic and manual starts use the same fresh Owner checks.
                start = vm.IsLabyrinthProfile ? PcrLabyrinthOwner.RequestStartAsync(vm) : PcrAlphaOwner.RequestStartAsync(vm);
            }
            await start;
        }
        catch (OperationCanceledException) when (cancel.IsCancellationRequested)
        {
            vm.Processor.AddLog("启动后操作已取消。", useMarkdown: false);
        }
        catch (Exception ex)
        {
            var message = "启动后操作未完成：" + ex.Message;
            vm.ShowPcrStartError(vm.BeginPcrRunFeedback(), message);
            vm.Processor.AddLog(message, useMarkdown: false);
            vm.FinishPlayerLog("not_started");
        }
        finally
        {
            Action? stopped = null;
            lock (PcrProfileRunGate.StartGate)
            {
                if (ReferenceEquals(launching, vm) && ReferenceEquals(cancellation, cancel))
                {
                    launching = null;
                    cancellation = null;
                    vm.IsRunning = false;
                    stopped = afterCancellation;
                    afterCancellation = null;
                }
                PcrProfileRunGate.Notify();
            }
            stopped?.Invoke();
        }
    }

    internal static bool Cancel(TaskQueueViewModel vm, Action? action)
    {
        lock (PcrProfileRunGate.StartGate)
        {
            if (!ReferenceEquals(launching, vm)) return false;
            afterCancellation += action;
            cancellation!.Cancel();
            return true;
        }
    }
}
