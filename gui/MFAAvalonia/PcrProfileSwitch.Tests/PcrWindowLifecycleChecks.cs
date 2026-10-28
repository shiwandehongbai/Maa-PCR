using System;
using System.Collections.Concurrent;
using System.Linq;
using System.Reflection;
using System.Runtime.CompilerServices;
using Avalonia;
using Avalonia.Interactivity;
using Avalonia.Threading;
using MFAAvalonia.Helper;
using MFAAvalonia.Pcr;
using MFAAvalonia.Views.Pages;

internal static class PcrWindowLifecycleChecks
{
    private const BindingFlags Hidden = BindingFlags.Instance | BindingFlags.Static | BindingFlags.NonPublic;
    private static readonly FieldInfo LanguageEvent = typeof(LanguageHelper).GetField("LanguageChanged", Hidden)!;
    private static readonly MethodInfo Loaded = typeof(TaskQueueView).GetMethod("OnLoaded", Hidden | BindingFlags.DeclaredOnly)!;
    private static readonly MethodInfo Unloaded = typeof(TaskQueueView).GetMethod("OnUnloaded", Hidden | BindingFlags.DeclaredOnly)!;

    internal static void Run()
    {
        var original = LanguageEvent.GetValue(null);
        var checks = 0;
        void Require(bool value, string message)
        {
            if (!value) throw new InvalidOperationException("window_lifecycle: " + message);
            checks++;
        }
        try
        {
            ExerciseSubscriptions(Require);
            var released = CreateReleasedView();
            for (var attempt = 0; attempt < 3 && released.IsAlive; attempt++)
            {
                Dispatcher.UIThread.RunJobs();
                GC.Collect();
                GC.WaitForPendingFinalizers();
                GC.Collect();
            }
            Require(!released.IsAlive, "unloaded page remains retained after queued work and collection");
            Console.WriteLine($"PCR_WINDOW_LIFECYCLE_CHECKS={checks}");
        }
        finally
        {
            LanguageEvent.SetValue(null, original);
            Dispatcher.UIThread.RunJobs();
        }
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private static void ExerciseSubscriptions(Action<bool, string> require)
    {
        var view = CreateView();
        try
        {
            for (var cycle = 0; cycle < 3; cycle++)
            {
                Loaded.Invoke(view, [null, new RoutedEventArgs()]);
                require(Subscriptions(view) == 1, "loaded page must subscribe exactly once");
                Loaded.Invoke(view, [null, new RoutedEventArgs()]);
                require(Subscriptions(view) == 1, "duplicate load must not duplicate the static subscription");
                Unloaded.Invoke(view, [null, new RoutedEventArgs()]);
                require(Subscriptions(view) == 0, "unloaded page must leave no static subscription");
                Unloaded.Invoke(view, [null, new RoutedEventArgs()]);
                require(Subscriptions(view) == 0, "duplicate unload must remain detached");
                Dispatcher.UIThread.RunJobs();
            }
        }
        finally
        {
            Unloaded.Invoke(view, [null, new RoutedEventArgs()]);
            Dispatcher.UIThread.RunJobs();
        }
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private static WeakReference CreateReleasedView()
    {
        var view = CreateView();
        Loaded.Invoke(view, [null, new RoutedEventArgs()]);
        Unloaded.Invoke(view, [null, new RoutedEventArgs()]);
        Dispatcher.UIThread.RunJobs();
        return new WeakReference(view);
    }

    private static int Subscriptions(TaskQueueView view) =>
        ((MulticastDelegate?)LanguageEvent.GetValue(null))?.GetInvocationList()
            .Count(handler => ReferenceEquals(handler.Target, view)) ?? 0;

    private static TaskQueueView CreateView()
    {
        var cache = (ConcurrentDictionary<Type, Lazy<object>>)typeof(Instances).GetField("ServiceCache", Hidden)!.GetValue(null)!;
        var rootType = typeof(PcrAlphaOwner).Assembly.GetType("MFAAvalonia.ViewModels.Windows.RootViewModel", true)!;
        var hadRoot = cache.TryGetValue(rootType, out var previousRoot);
        cache[rootType] = new Lazy<object>(() => RuntimeHelpers.GetUninitializedObject(rootType));
        var resources = Application.Current!.Resources;
        var hadPlugins = resources.TryGetValue("MdXamlPlugin", out var previousPlugins);
        resources["MdXamlPlugin"] = new Markdown.Avalonia.MdAvPlugins();
        try { return new TaskQueueView(); }
        finally
        {
            if (hadRoot) cache[rootType] = previousRoot!; else cache.TryRemove(rootType, out _);
            if (hadPlugins) resources["MdXamlPlugin"] = previousPlugins!; else resources.Remove("MdXamlPlugin");
        }
    }
}
