using System;
using System.IO;

namespace WastedBridge.OfflineChecks
{
    /// <summary>Repo layout, resolved from the tool's own location so nothing is hard-coded.</summary>
    internal static class Paths
    {
        public static string BridgeRoot { get; private set; }
        public static string ToolRoot { get; private set; }

        public static string CompiledBridgeDir
        {
            get { return Path.Combine(BridgeRoot, "bin", "Release", "net48"); }
        }

        public static string CompiledBridgeDll
        {
            get { return Path.Combine(CompiledBridgeDir, "WastedBridge.dll"); }
        }

        public static string RealShvdnDll
        {
            get { return Path.Combine(BridgeRoot, "lib", "ScriptHookVDotNet3.dll"); }
        }

        public static string ContractSamplesDir
        {
            get { return Path.Combine(BridgeRoot, "contract-samples"); }
        }

        public static string StubDir(string name)
        {
            return Path.Combine(ToolRoot, name, "bin", "Release", "netstandard2.0");
        }

        public static void Resolve()
        {
            var dir = new DirectoryInfo(AppContext.BaseDirectory);
            while (dir != null && !File.Exists(Path.Combine(dir.FullName, "WastedBridge.csproj")))
            {
                dir = dir.Parent;
            }
            if (dir == null)
            {
                throw new InvalidOperationException(
                    "could not locate bridge/WastedBridge.csproj above " + AppContext.BaseDirectory);
            }
            BridgeRoot = dir.FullName;
            ToolRoot = Path.Combine(BridgeRoot, "tools", "offline-checks");
        }
    }
}
