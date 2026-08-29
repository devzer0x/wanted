using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;

namespace WastedBridge.OfflineChecks
{
    /// <summary>
    /// Metadata-only reads from the compiled WastedBridge.dll. Used for constants that live on
    /// types the stub cannot satisfy (WastedBridgeScript derives from GTA.Script, which the SHVDN
    /// stub does not define, so the type cannot be *loaded* — but its metadata can be read).
    /// </summary>
    internal static class BridgeMetadata
    {
        public static string ReadConstString(string typeName, string fieldName)
        {
            var paths = new List<string> { Paths.CompiledBridgeDll };
            paths.AddRange(Directory.GetFiles(Paths.CompiledBridgeDir, "*.dll"));
            if (Directory.Exists(BridgeAssembly.StubDirectory ?? ""))
            {
                paths.AddRange(Directory.GetFiles(BridgeAssembly.StubDirectory, "*.dll"));
            }
            paths.AddRange(Directory.GetFiles(
                System.Runtime.InteropServices.RuntimeEnvironment.GetRuntimeDirectory(), "*.dll"));

            using (var mlc = new MetadataLoadContext(new PathAssemblyResolver(paths),
                                                     "System.Private.CoreLib"))
            {
                Assembly asm = mlc.LoadFromAssemblyPath(Paths.CompiledBridgeDll);
                Type t = asm.GetType(typeName, true);
                FieldInfo f = t.GetField(fieldName,
                    BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
                if (f == null)
                {
                    throw new MissingFieldException(typeName, fieldName);
                }
                return (string)f.GetRawConstantValue();
            }
        }
    }
}
