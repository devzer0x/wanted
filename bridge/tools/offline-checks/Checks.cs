using System;

namespace WastedBridge.OfflineChecks
{
    /// <summary>PASS/FAIL accounting shared by every section of the harness.</summary>
    internal static class Checks
    {
        public static int Passed;
        public static int Failed;

        public static void Report(bool ok, string name, string detail)
        {
            Console.WriteLine((ok ? "PASS" : "FAIL") + "  " + name + "  -> " + detail);
            if (ok)
            {
                Passed++;
            }
            else
            {
                Failed++;
            }
        }
    }
}
