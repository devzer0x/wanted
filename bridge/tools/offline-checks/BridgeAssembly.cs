using System;
using System.IO;
using System.Reflection;
using System.Runtime.Loader;

namespace WastedBridge.OfflineChecks
{
    /// <summary>
    /// Loads the compiled net48 WastedBridge.dll into this .NET 8 process and exposes small
    /// reflection helpers. Everything the checks touch is the real compiled code — the only
    /// substitution is the SHVDN stub the loader resolves GTA types against (see StubGenerator).
    /// </summary>
    internal static class BridgeAssembly
    {
        private static Assembly _asm;

        public static string StubDirectory { get; private set; }

        public static Assembly Load(string stubDirectory)
        {
            if (_asm != null)
            {
                return _asm;
            }
            StubDirectory = stubDirectory;
            string bridgeDir = Paths.CompiledBridgeDir;

            AssemblyLoadContext.Default.Resolving += (ctx, name) =>
            {
                foreach (string dir in new[] { bridgeDir, stubDirectory })
                {
                    string candidate = Path.Combine(dir, name.Name + ".dll");
                    if (File.Exists(candidate))
                    {
                        return ctx.LoadFromAssemblyPath(candidate);
                    }
                }
                return null;
            };

            _asm = AssemblyLoadContext.Default.LoadFromAssemblyPath(Paths.CompiledBridgeDll);
            return _asm;
        }

        public static Assembly Asm
        {
            get
            {
                if (_asm == null)
                {
                    throw new InvalidOperationException("BridgeAssembly.Load was not called");
                }
                return _asm;
            }
        }

        public static Type T(string name)
        {
            return Asm.GetType("WastedBridge." + name, true);
        }

        public static object New(string typeName)
        {
            return Activator.CreateInstance(T(typeName), true);
        }

        public static object Call(Type type, string method, params object[] args)
        {
            MethodInfo m = type.GetMethod(method,
                BindingFlags.Public | BindingFlags.NonPublic
                | BindingFlags.Static | BindingFlags.Instance);
            if (m == null)
            {
                throw new MissingMethodException(type.FullName, method);
            }
            return m.IsStatic ? m.Invoke(null, args) : m.Invoke(args[0], SubArgs(args));
        }

        public static object CallStatic(string typeName, string method, params object[] args)
        {
            return Call(T(typeName), method, args);
        }

        public static object CallInstance(object target, string method, params object[] args)
        {
            MethodInfo m = target.GetType().GetMethod(method,
                BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
            if (m == null)
            {
                throw new MissingMethodException(target.GetType().FullName, method);
            }
            return m.Invoke(target, args);
        }

        public static object GetProp(object target, string name)
        {
            return target.GetType().GetProperty(name,
                BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)
                .GetValue(target);
        }

        public static void SetProp(object target, string name, object value)
        {
            target.GetType().GetProperty(name,
                BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)
                .SetValue(target, value);
        }

        /// <summary>Sets one public field on a DTO instance from the bridge assembly.</summary>
        public static void SetField(object target, string field, object value)
        {
            FieldInfo f = target.GetType().GetField(field,
                BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
            if (f == null)
            {
                throw new MissingFieldException(target.GetType().FullName, field);
            }
            f.SetValue(target, value);
        }

        private static object[] SubArgs(object[] args)
        {
            var rest = new object[args.Length - 1];
            Array.Copy(args, 1, rest, 0, rest.Length);
            return rest;
        }
    }
}
