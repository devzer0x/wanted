using System;
using System.Globalization;
using System.IO;
using System.Text;

namespace WastedBridge.OfflineChecks
{
    /// <summary>
    /// Writes bridge/contract-samples/README.md with the provenance of the sample files. Generated
    /// rather than hand-maintained so it cannot drift from how the samples were actually produced.
    /// </summary>
    internal static class SamplesReadme
    {
        public static void Write()
        {
            string dir = Paths.ContractSamplesDir;
            var sb = new StringBuilder();

            sb.AppendLine("# contract-samples — real serializations of the bridge's JSON");
            sb.AppendLine();
            sb.AppendLine("**Generated file. Do not edit by hand.** Everything here is rewritten by");
            sb.AppendLine("`bridge/tools/offline-checks/run.sh`; edit the tool, not the output.");
            sb.AppendLine();
            sb.AppendLine("These files exist so the harness package can test its pydantic models against the");
            sb.AppendLine("bytes the bridge really emits, instead of against a hand-typed guess. Until they");
            sb.AppendLine("existed, the offline checks covered routing and transport but never serialized a");
            sb.AppendLine("`Snapshot` — which is exactly how the `last_task.id: null` mismatch survived to");
            sb.AppendLine("delivery day (CONTRACTS v1.2 now blesses those nulls).");
            sb.AppendLine();
            sb.AppendLine("## How they were produced");
            sb.AppendLine();
            sb.AppendLine("1. `dotnet build -c Release --no-incremental` compiles `bridge/` to");
            sb.AppendLine("   `bin/Release/net48/WastedBridge.dll`.");
            sb.AppendLine("2. `bridge/tools/offline-checks` loads **that compiled DLL** under .NET 8, builds");
            sb.AppendLine("   `WastedBridge.Snapshot` object graphs out of its own DTO types by reflection, and");
            sb.AppendLine("   serializes them by calling the bridge's own `SnapshotJson.Serialize` — the exact");
            sb.AppendLine("   method the game thread publishes `/state` with (`WastedBridgeScript.TickCore`).");
            sb.AppendLine("3. `/health` and every offline-reachable error body are captured from the real");
            sb.AppendLine("   `BridgeRouter` over a real HTTP connection to `127.0.0.1:7777`.");
            sb.AppendLine("4. Each document is then checked field-by-field against CONTRACTS §1 before it is");
            sb.AppendLine("   written; the tool exits non-zero if any documented field is missing or has the");
            sb.AppendLine("   wrong JSON type.");
            sb.AppendLine();
            sb.AppendLine("Regenerate with:");
            sb.AppendLine();
            sb.AppendLine("```");
            sb.AppendLine("bridge/tools/offline-checks/run.sh");
            sb.AppendLine("```");
            sb.AppendLine();
            sb.AppendLine("## What is real and what is not");
            sb.AppendLine();
            sb.AppendLine("| Aspect | Status |");
            sb.AppendLine("|---|---|");
            sb.AppendLine("| Field names, nesting, JSON types, null handling, number formatting | **Real** — produced by the compiled assembly's DTOs and serializer settings |");
            sb.AppendLine("| `/health` and `errors.json` bodies | **Real** — HTTP responses from the compiled router |");
            sb.AppendLine("| `last_task` in `state-fresh-load.json` | **Real** — `TaskEngine.ToDto()` on a never-tasked engine |");
            sb.AppendLine("| The world *values* (positions, models, street names, tick, ts) | **Synthetic** — natives cannot run off the game server, so the object graphs are assembled by the tool |");
            sb.AppendLine();
            sb.AppendLine("A sample captured from a live session is a Phase-1 server deliverable");
            sb.AppendLine("(`scripts/bridge-smoke.ps1` with the game running); these are shape fixtures, not");
            sb.AppendLine("recordings, and must never be presented as one.");
            sb.AppendLine();
            sb.AppendLine("## Files");
            sb.AppendLine();
            sb.AppendLine("| File | What it is |");
            sb.AppendLine("|---|---|");
            sb.AppendLine("| `state-fresh-load.json` | `GET /state` on the first poll of a session: no task ever posted (`last_task.id`/`type` **null**, status `idle`), player on foot (`vehicle: null`), no objective blip, `bridge.edition: \"unknown\"`. The document that crashed the harness before v1.2. |");
            sb.AppendLine("| `state-populated.json` | `GET /state` with every optional branch present: in a vehicle, one nearby vehicle and ped, a mission with an `objective_blip`, a `running` `drive_to`, `edition: \"legacy\"`. |");
            sb.AppendLine("| `health-fresh-load.json` | `GET /health` before the first tick — `edition: \"unknown\"`, `tick_hz: 0`. |");
            sb.AppendLine("| `errors.json` | Catalog of real error responses, one per CONTRACTS v1.2 code reachable without the game, plus the bridge-side extras (`not_found`, `method_not_allowed`). `not_in_vehicle` is game-thread-only and is listed under `not_capturable_offline`. |");
            sb.AppendLine();
            sb.AppendLine("`state-*.json` and `health-*.json` are written **verbatim**: byte-for-byte the HTTP");
            sb.AppendLine("response body, compact, with no trailing newline. `errors.json` is a catalog wrapper");
            sb.AppendLine("(indented) whose `captured[].body` values are the verbatim response bodies.");
            sb.AppendLine();
            sb.AppendLine("## Provenance of this run");
            sb.AppendLine();
            sb.AppendLine("| | |");
            sb.AppendLine("|---|---|");
            sb.AppendLine("| generated (UTC) | " + DateTime.UtcNow.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture) + " |");
            sb.AppendLine("| tool | `bridge/tools/offline-checks` on .NET " + Environment.Version + " (" + System.Runtime.InteropServices.RuntimeInformation.RuntimeIdentifier + ") |");
            sb.AppendLine("| bridge DLL | `" + Rel(Paths.CompiledBridgeDll) + "` |");
            sb.AppendLine("| bridge DLL sha256 | `" + Sha(Paths.CompiledBridgeDll) + "` |");
            sb.AppendLine("| bridge DLL built | " + Built(Paths.CompiledBridgeDll) + " |");
            sb.AppendLine("| SHVDN reference | `" + Rel(Paths.RealShvdnDll) + "` sha256 `" + Sha(Paths.RealShvdnDll) + "` |");
            sb.AppendLine();
            foreach (string name in new[]
                     {
                         "state-fresh-load.json", "state-populated.json",
                         "health-fresh-load.json", "errors.json"
                     })
            {
                string path = Path.Combine(dir, name);
                sb.AppendLine("- `" + name + "` — " + (File.Exists(path)
                    ? new FileInfo(path).Length + " bytes"
                    : "MISSING"));
            }

            string readme = Path.Combine(dir, "README.md");
            File.WriteAllText(readme, sb.ToString());
            Checks.Report(File.Exists(readme), "[samples] wrote README.md", readme);
        }

        private static string Rel(string path)
        {
            string root = Directory.GetParent(Paths.BridgeRoot).FullName;
            return path.StartsWith(root, StringComparison.Ordinal)
                ? path.Substring(root.Length).TrimStart('/')
                : path;
        }

        private static string Sha(string path)
        {
            return File.Exists(path) ? StubGenerator.Sha256(path) : "<file missing>";
        }

        private static string Built(string path)
        {
            return File.Exists(path)
                ? File.GetLastWriteTimeUtc(path).ToString("yyyy-MM-dd HH:mm:ss",
                    CultureInfo.InvariantCulture) + " UTC"
                : "<file missing>";
        }
    }
}
