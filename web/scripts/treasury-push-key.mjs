#!/usr/bin/env node
// Moves the treasury private key from its encrypted keystore into Vercel Production, and nowhere
// else. docs/CONTRACTS-PREDICTIONS.md §10.7 is the procedure; docs/RUNBOOK.md §5.1 walks the
// operator through it. Run it in YOUR OWN terminal on the Mac, never through an AI session and
// never in a terminal that is being recorded or transcribed:
//
//     cd web
//     node scripts/treasury-push-key.mjs --keystore ~/.foundry/keystores/wanted-treasury \
//       --expect 0x<address> --dry-run          # decrypts and checks, pushes nothing
//     node scripts/treasury-push-key.mjs --keystore ~/.foundry/keystores/wanted-treasury \
//       --expect 0x<address>                    # the real push
//
// WHAT IT DOES
//
//   1. Reads the scrypt keystore v3 file that `cast wallet new ~/.foundry/keystores
//      wanted-treasury` wrote. Only kdf `scrypt` and cipher `aes-128-ctr` are accepted: those
//      are what cast writes, and a keystore claiming anything else is not one we made.
//   2. Asks for the passphrase. On a terminal: a hidden prompt (raw mode, nothing echoed,
//      backspace works, Ctrl-C cancels and puts the terminal back). Not on a terminal: exactly
//      one line read from stdin. Never from argv or the environment — both are readable by other
//      processes (`ps`, /proc, crash reporters) and argv lands in shell history.
//   3. Derives the key with node:crypto only (scryptSync + aes-128-ctr), after checking the MAC,
//      keccak256(dk[16..32] ++ ciphertext). A wrong passphrase is a MAC mismatch and a refusal.
//      Without the MAC check a wrong passphrase would still "decrypt" — to 32 bytes of garbage
//      that make a perfectly valid, wrong, key.
//   4. Rejects scalar 0 and anything >= the secp256k1 order before the key reaches viem, then
//      derives the address with viem's privateKeyToAccount, the same function the payout worker
//      signs with (src/lib/rewards/treasury.ts).
//   5. Refuses unless that address equals --expect, which is required, and the keystore's own
//      `address` field when the file carries one (see "THE ADDRESS CHECKS").
//   6. Without --dry-run, runs `npx vercel@59.10.0 env add TREASURY_PRIVATE_KEY production --sensitive
//      --force` with the key on the child's STDIN, then `vercel env add
//      NEXT_PUBLIC_TREASURY_ADDRESS production --no-sensitive --force` with the address.
//
// WHY EACH RULE EXISTS
//
// The key lives in exactly two places: Vercel Production as a Sensitive variable (write-only
// once set) and the encrypted keystore. Anything that makes a third copy defeats that, so:
//
//   * Nothing prints but the address and vercel's exit status. The key is never printed, not
//     even in an error: every refusal is a fixed sentence, and no message from node:crypto, viem,
//     parseArgs or the child is ever passed through unfiltered (parseArgs, for one, echoes the
//     offending argument, which is exactly where a mis-pasted key would be).
//   * Child output is scanned and every run of 64 or more hex digits becomes [redacted] before it
//     is shown. The vercel CLI does not echo the value today; this does not rely on that.
//   * The key reaches vercel on stdin, never as `--value <key>`: argv is visible to `ps` for as
//     long as the child runs. It goes as one 66-byte write with no trailing newline, because
//     vercel 59.10.0 (dist/chunks, readStandardInput) takes the FIRST stdin chunk that arrives
//     within 500 ms as the whole value; a small single write is one chunk.
//   * Nothing touches the disk: no temp files, no .env write, no clipboard. Every Buffer that
//     held the passphrase, the derived scrypt key, the private key or its hex text is zero-filled
//     after use, including on refusal.
//   * Production only, and --sensitive. Preview and Development never get the key, so a PR
//     preview or a dev box can never sign (isTreasuryConfigured() is false there).
//   * --force replaces an existing value for the same target: that is what a rotation is
//     (RUNBOOK §5, "rotation"). Without it a second push would fail on the existing variable.
//   * NEXT_PUBLIC_TREASURY_ADDRESS is public by design (the /rules page shows it), so it is stored
//     with --no-sensitive. vercel refuses a NEXT_PUBLIC_ variable as a Secret anyway, since the
//     prefix inlines it into the browser bundle.
//
// THE ADDRESS CHECKS
//
// --expect is the address the operator saw when `cast wallet new` created the keystore, and again
// from `cast wallet address --keystore` (an independent decryptor). Requiring it means a restore
// of the wrong file, or the wrong passphrase on a keystore that somehow passes its MAC, can never
// silently push a key nobody funded or recorded. If the keystore also carries an `address` field
// (geth and some other tools write one), it must agree too. cast 1.6.0 does NOT write that field
// (checked 2026-09-14 against a throwaway `cast wallet new` keystore: keys crypto, id, version),
// so for the keystores this procedure makes, --expect is the check that binds.
//
// WHAT JAVASCRIPT CANNOT WIPE
//
// Buffers can be zeroed; strings cannot. viem's privateKeyToAccount takes the key as a hex string,
// so one immutable copy exists on the heap until the garbage collector reclaims it, as do the
// internal states of OpenSSL's scrypt/AES and noble's keccak. The process exits seconds later and
// nothing is written to swap on purpose; that is the honest limit of doing this in Node.

import { spawnSync } from "node:child_process";
import { createDecipheriv, scryptSync, timingSafeEqual } from "node:crypto";
import { existsSync, readFileSync, readSync } from "node:fs";
import { join } from "node:path";
import { isatty } from "node:tty";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";

import { keccak256 } from "viem";
import { privateKeyToAccount } from "viem/accounts";

const webRoot = join(fileURLToPath(new URL(".", import.meta.url)), "..");

// The Vercel team that owns the linked project (web/.vercel/project.json). Passed explicitly so a
// CLI logged into a personal account, or another team, cannot write the key somewhere else.
const VERCEL_SCOPE = "memewars-projects";

// secp256k1 group order n, big-endian (SEC 2 v2 §2.4.1). A private key must be in [1, n).
const SECP256K1_N = Buffer.from(
  "fffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141",
  "hex",
);

const MAX_PASSPHRASE_BYTES = 1024;
const MAX_KEYSTORE_BYTES = 64 * 1024;
// A keystore's scrypt parameters come from the file, so they are bounded before any memory is
// committed: 128*N*r bytes is what scrypt allocates. geth's "standard" (N=2^18, r=8) is 256 MiB,
// the largest a real keystore uses; cast writes N=2^13, r=8 (8 MiB).
const MAX_SCRYPT_BYTES = 256 * 1024 * 1024;

const USAGE =
  "usage: node scripts/treasury-push-key.mjs --keystore <path> --expect 0x<address> [--dry-run]\n" +
  "the passphrase is read from a hidden prompt, or one line of stdin when stdin is not a terminal";

class Refusal extends Error {
  constructor(message, exitCode = 1) {
    super(message);
    this.exitCode = exitCode;
  }
}

function strictHex(value, bytes) {
  if (typeof value !== "string") return null;
  const hex = value.startsWith("0x") ? value.slice(2) : value;
  if (hex.length === 0 || hex.length % 2 !== 0 || !/^[0-9a-fA-F]+$/.test(hex)) return null;
  if (bytes !== undefined && hex.length !== bytes * 2) return null;
  return Buffer.from(hex, "hex");
}

function parseCli() {
  let parsed;
  try {
    parsed = parseArgs({
      options: {
        keystore: { type: "string" },
        expect: { type: "string" },
        "dry-run": { type: "boolean", default: false },
      },
      strict: true,
      allowPositionals: false,
    });
  } catch {
    // parseArgs' own message quotes the offending token. Never show it: if someone pasted a key
    // or a passphrase onto the command line, that token is the secret.
    throw new Refusal(`refused: unrecognised arguments (nothing echoed)\n${USAGE}`, 2);
  }
  const { keystore, expect } = parsed.values;
  if (!keystore) throw new Refusal(`refused: --keystore is required\n${USAGE}`, 2);
  if (!expect) {
    throw new Refusal(
      "refused: --expect is required — the address `cast wallet new` printed when it made this keystore\n" +
        USAGE,
      2,
    );
  }
  if (!/^0x[0-9a-fA-F]{40}$/.test(expect)) {
    throw new Refusal("refused: --expect must be 0x followed by 40 hex digits (value not echoed)", 2);
  }
  return { keystorePath: keystore, expect: expect.toLowerCase(), dryRun: parsed.values["dry-run"] };
}

function loadKeystore(path) {
  let raw;
  try {
    raw = readFileSync(path);
  } catch {
    throw new Refusal("refused: cannot read the keystore file");
  }
  if (raw.length > MAX_KEYSTORE_BYTES) throw new Refusal("refused: file is too large to be a keystore");
  let ks;
  try {
    ks = JSON.parse(raw.toString("utf8"));
  } catch {
    throw new Refusal("refused: keystore is not valid JSON");
  }
  const crypto = ks?.crypto ?? ks?.Crypto;
  if (ks?.version !== 3 || typeof crypto !== "object" || crypto === null) {
    throw new Refusal("refused: not a version 3 keystore");
  }
  if (crypto.kdf !== "scrypt") throw new Refusal("refused: unsupported kdf (only scrypt is accepted)");
  if (crypto.cipher !== "aes-128-ctr") {
    throw new Refusal("refused: unsupported cipher (only aes-128-ctr is accepted)");
  }
  const kp = crypto.kdfparams ?? {};
  const { n, r, p, dklen } = kp;
  const isPosInt = (v) => Number.isSafeInteger(v) && v > 0;
  if (!isPosInt(n) || !isPosInt(r) || !isPosInt(p) || n < 2 || (n & (n - 1)) !== 0 || n > 2 ** 30) {
    throw new Refusal("refused: invalid scrypt parameters");
  }
  if (dklen !== 32) throw new Refusal("refused: invalid scrypt parameters (dklen must be 32)");
  if (128 * n * r > MAX_SCRYPT_BYTES || p > 16) {
    throw new Refusal("refused: scrypt parameters exceed the memory bound");
  }
  const salt = strictHex(kp.salt);
  const iv = strictHex(crypto.cipherparams?.iv, 16);
  const ciphertext = strictHex(crypto.ciphertext, 32);
  const mac = strictHex(crypto.mac, 32);
  if (!salt || salt.length > 128 || !iv || !ciphertext || !mac) {
    throw new Refusal("refused: malformed keystore fields");
  }
  let address = null;
  if (ks.address !== undefined) {
    const a = strictHex(ks.address, 20);
    if (!a) throw new Refusal("refused: keystore address field is malformed");
    address = `0x${a.toString("hex")}`;
  }
  return { n, r, p, dklen, salt, iv, ciphertext, mac, address };
}

// ---- passphrase input ------------------------------------------------------------------------

/** Not a terminal: consume exactly one line from fd 0, one byte at a time so nothing beyond it is
 * read. process.stdin is never touched on this path — creating that stream can switch the pipe to
 * non-blocking mode, and it buffers input this code could not then wipe. */
function readPassphraseLine() {
  const buf = Buffer.alloc(MAX_PASSPHRASE_BYTES + 1);
  const one = Buffer.alloc(1);
  const pause = new Int32Array(new SharedArrayBuffer(4));
  let len = 0;
  try {
    for (;;) {
      let got;
      try {
        got = readSync(0, one, 0, 1, null);
      } catch (e) {
        if (e?.code === "EAGAIN") {
          Atomics.wait(pause, 0, 0, 20);
          continue;
        }
        if (e?.code === "EOF") got = 0;
        else throw new Refusal("refused: cannot read the passphrase from stdin");
      }
      if (got === 0) break;
      if (one[0] === 0x0a) break;
      if (len === MAX_PASSPHRASE_BYTES) {
        throw new Refusal(`refused: passphrase line is longer than ${MAX_PASSPHRASE_BYTES} bytes`);
      }
      buf[len++] = one[0];
    }
    if (len > 0 && buf[len - 1] === 0x0d) buf[--len] = 0;
    if (len === 0) throw new Refusal("refused: no passphrase on stdin");
    const out = Buffer.alloc(len);
    buf.copy(out, 0, 0, len);
    return out;
  } finally {
    buf.fill(0);
    one.fill(0);
  }
}

/** A terminal: hidden prompt in raw mode. Raw mode turns echo off and delivers Ctrl-C as byte 0x03
 * instead of SIGINT, so this handles it and restores the terminal on every way out. */
function promptPassphrase() {
  return new Promise((resolve, reject) => {
    const stdin = process.stdin;
    const buf = Buffer.alloc(MAX_PASSPHRASE_BYTES);
    let len = 0;
    let done = false;

    const restoreTerminal = () => {
      try {
        stdin.setRawMode(false);
      } catch {
        // the terminal is already gone; nothing left to restore
      }
    };
    const onSignal = (signal) => {
      restoreTerminal();
      buf.fill(0);
      process.stderr.write("\n");
      process.exit(signal === "SIGHUP" ? 129 : 143);
    };
    const finish = (err) => {
      if (done) return;
      done = true;
      stdin.removeListener("data", onData);
      stdin.removeListener("end", onEnd);
      stdin.removeListener("error", onEnd);
      process.removeListener("exit", restoreTerminal);
      process.removeListener("SIGTERM", onSignal);
      process.removeListener("SIGHUP", onSignal);
      restoreTerminal();
      stdin.pause();
      process.stderr.write("\n");
      if (err) {
        buf.fill(0);
        reject(err);
        return;
      }
      const out = Buffer.alloc(len);
      buf.copy(out, 0, 0, len);
      buf.fill(0);
      if (len === 0) {
        out.fill(0);
        reject(new Refusal("refused: empty passphrase"));
        return;
      }
      resolve(out);
    };
    const onEnd = () => finish(new Refusal("refused: no passphrase entered"));
    function onData(chunk) {
      try {
        for (let i = 0; i < chunk.length && !done; i++) {
          const b = chunk[i];
          if (b === 0x03) return finish(new Refusal("cancelled", 130)); // Ctrl-C
          if (b === 0x04) {
            if (len === 0) return finish(new Refusal("cancelled", 130)); // Ctrl-D on an empty line
            continue;
          }
          if (b === 0x0d || b === 0x0a) return finish(null); // Enter
          if (b === 0x7f || b === 0x08) {
            // Backspace removes one whole UTF-8 character: its continuation bytes, then its lead.
            while (len > 0 && (buf[len - 1] & 0xc0) === 0x80) buf[--len] = 0;
            if (len > 0) buf[--len] = 0;
            continue;
          }
          if (b === 0x15) {
            buf.fill(0, 0, len); // Ctrl-U clears the line, as it does at a shell prompt
            len = 0;
            continue;
          }
          if (b === 0x1b) break; // an escape sequence (arrow keys etc.): drop the rest of the chunk
          if (b < 0x20) continue; // other control bytes are never part of a passphrase
          if (len === MAX_PASSPHRASE_BYTES) {
            return finish(new Refusal(`refused: passphrase is longer than ${MAX_PASSPHRASE_BYTES} bytes`));
          }
          buf[len++] = b;
        }
      } finally {
        chunk.fill(0);
      }
    }

    process.on("exit", restoreTerminal);
    process.on("SIGTERM", onSignal);
    process.on("SIGHUP", onSignal);
    process.stderr.write("Keystore passphrase (hidden): ");
    stdin.setRawMode(true);
    stdin.on("data", onData);
    stdin.on("end", onEnd);
    stdin.on("error", onEnd);
    stdin.resume();
  });
}

// ---- decryption ------------------------------------------------------------------------------

function scalarInRange(key) {
  let nonZero = 0;
  for (const b of key) nonZero |= b;
  if (nonZero === 0) return false;
  for (let i = 0; i < 32; i++) {
    if (key[i] < SECP256K1_N[i]) return true;
    if (key[i] > SECP256K1_N[i]) return false;
  }
  return false; // exactly n
}

/** Returns a fresh 32-byte Buffer holding the private key. The caller zero-fills it. */
function decryptKeystore(ks, passphrase) {
  let dk = null;
  const macInput = Buffer.alloc(16 + ks.ciphertext.length);
  let computed = null;
  let head = null;
  let tail = null;
  try {
    try {
      dk = scryptSync(passphrase, ks.salt, ks.dklen, {
        N: ks.n,
        r: ks.r,
        p: ks.p,
        maxmem: 256 * ks.n * ks.r,
      });
    } catch {
      throw new Refusal("refused: scrypt failed on this keystore's parameters");
    }
    dk.copy(macInput, 0, 16, 32);
    ks.ciphertext.copy(macInput, 16);
    computed = keccak256(macInput, "bytes");
    if (!timingSafeEqual(Buffer.from(computed.buffer, computed.byteOffset, 32), ks.mac)) {
      throw new Refusal("refused: wrong passphrase (keystore MAC does not match)");
    }
    const decipher = createDecipheriv("aes-128-ctr", dk.subarray(0, 16), ks.iv);
    head = decipher.update(ks.ciphertext);
    tail = decipher.final();
    if (head.length + tail.length !== 32) throw new Refusal("refused: keystore does not hold a 32-byte key");
    const key = Buffer.alloc(32);
    head.copy(key, 0);
    tail.copy(key, head.length);
    return key;
  } finally {
    dk?.fill(0);
    macInput.fill(0);
    computed?.fill(0);
    head?.fill(0);
    tail?.fill(0);
  }
}

const HEX_DIGITS = Buffer.from("0123456789abcdef", "latin1");

/** `0x` + 64 lowercase hex digits, built into a Buffer (not a string) so it can be wiped. */
function keyHexBuffer(key) {
  const out = Buffer.alloc(2 + key.length * 2);
  out[0] = 0x30; // '0'
  out[1] = 0x78; // 'x'
  for (let i = 0; i < key.length; i++) {
    out[2 + 2 * i] = HEX_DIGITS[key[i] >> 4];
    out[3 + 2 * i] = HEX_DIGITS[key[i] & 0x0f];
  }
  return out;
}

// ---- vercel ----------------------------------------------------------------------------------

const redact = (text) => text.replace(/[0-9a-fA-F]{64,}/g, "[redacted]");

/** The exact Vercel CLI every claim in this file was checked against (FS3, 2026-09-15 review).
 * `npx vercel` unpinned installs whatever the registry calls latest when no local binary exists, and
 * the stdin behaviour and --sensitive semantics this tool depends on are properties of THIS version. */
const VERCEL_CLI = "vercel@59.10.0";

/** `vercel env add <name> production <flags> --force --scope <team>` from web/, value on stdin.
 * Every flag was checked against `npx vercel env add --help` on Vercel CLI 59.10.0 (2026-09-14):
 * --sensitive, --no-sensitive, --force, and the global -S/--scope. */
function vercelEnvAdd(name, flags, value) {
  const argv = [VERCEL_CLI, "env", "add", name, "production", ...flags, "--force", "--scope", VERCEL_SCOPE];
  const res = spawnSync("npx", argv, {
    cwd: webRoot,
    input: value,
    stdio: ["pipe", "pipe", "pipe"],
    timeout: 120_000,
    maxBuffer: 1024 * 1024,
  });
  const shown = `npx ${VERCEL_CLI} env add ${name} production ${flags.join(" ")} --force --scope ${VERCEL_SCOPE}`;
  if (res.error) {
    process.stderr.write(`${shown}: did not run (${String(res.error.code ?? "spawn_failed")})\n`);
    return false;
  }
  const output = redact(`${res.stdout?.toString("utf8") ?? ""}${res.stderr?.toString("utf8") ?? ""}`).trim();
  const status = res.status === null ? `signal ${res.signal}` : `exit ${res.status}`;
  process.stderr.write(`${shown}: ${status}\n`);
  if (output) process.stderr.write(`${output.replace(/^/gm, "  | ")}\n`);
  return res.status === 0;
}

/** The rows of `vercel env ls <target>`: [{name, type}]. 59.10.0 prints `name  value  type
 * environments  created`, and a --sensitive variable reads `Hidden  Secret` (checked 2026-09-15). */
function vercelEnvRows(target) {
  const res = spawnSync("npx", [VERCEL_CLI, "env", "ls", target, "--scope", VERCEL_SCOPE], {
    cwd: webRoot,
    stdio: ["ignore", "pipe", "pipe"],
    timeout: 120_000,
    maxBuffer: 1024 * 1024,
  });
  if (res.error || res.status !== 0) return null;
  const rows = [];
  for (const line of redact(res.stdout?.toString("utf8") ?? "").split("\n")) {
    const m = /^\s+([A-Z][A-Z0-9_]*)\s+(\S+)\s+(Secret|Config|Plain|Encrypted|Sensitive)\s/.exec(line);
    if (m) rows.push({ name: m[1], type: m[3] });
  }
  return rows;
}

/** After the push, prove where the key landed (FS3): Production has it as a Secret, and neither
 * Preview nor Development has it at all (§10.7). A push that "succeeded" into the wrong place, or as
 * readable config, must not be reported as done. */
function verifyPlacement() {
  const prod = vercelEnvRows("production");
  const preview = vercelEnvRows("preview");
  const dev = vercelEnvRows("development");
  if (!prod || !preview || !dev) return "could not list the project's environment variables";
  const key = prod.find((r) => r.name === "TREASURY_PRIVATE_KEY");
  if (!key) return "TREASURY_PRIVATE_KEY is not listed in Production";
  if (key.type !== "Secret") return `TREASURY_PRIVATE_KEY is stored as ${key.type}, not Secret`;
  if (!prod.some((r) => r.name === "NEXT_PUBLIC_TREASURY_ADDRESS")) return "NEXT_PUBLIC_TREASURY_ADDRESS is not listed in Production";
  if (preview.some((r) => r.name === "TREASURY_PRIVATE_KEY")) return "TREASURY_PRIVATE_KEY is present in Preview";
  if (dev.some((r) => r.name === "TREASURY_PRIVATE_KEY")) return "TREASURY_PRIVATE_KEY is present in Development";
  return null;
}

// ---- main ------------------------------------------------------------------------------------

async function main() {
  const { keystorePath, expect, dryRun } = parseCli();
  const ks = loadKeystore(keystorePath);
  if (!dryRun && !existsSync(join(webRoot, ".vercel", "project.json"))) {
    throw new Refusal("refused: web/ is not linked to the Vercel project (web/.vercel/project.json missing)");
  }

  let passphrase = null;
  let key = null;
  let keyHex = null;
  // FS4 (2026-09-15 review): the prompt restores the terminal on a signal, but an interrupt during the
  // decrypt or the up-to-120 s push used to skip the `finally` below. These handlers live as long as
  // any secret does: zero what is held, say so in a fixed sentence, and exit with the signal's code.
  const EXIT_FOR = { SIGINT: 130, SIGHUP: 129, SIGTERM: 143 };
  const onSignal = (sig) => {
    passphrase?.fill(0);
    key?.fill(0);
    keyHex?.fill(0);
    process.stderr.write(`treasury-push-key: interrupted (${sig}); secrets wiped. Check \`npx ${VERCEL_CLI} env ls production\` before retrying.\n`);
    process.exit(EXIT_FOR[sig] ?? 1);
  };
  for (const sig of Object.keys(EXIT_FOR)) process.on(sig, onSignal);
  try {
    passphrase = isatty(0) ? await promptPassphrase() : readPassphraseLine();
    key = decryptKeystore(ks, passphrase);
    passphrase.fill(0);
    if (!scalarInRange(key)) {
      throw new Refusal("refused: decrypted key is not a valid secp256k1 scalar (0 or >= n)");
    }
    keyHex = keyHexBuffer(key);
    key.fill(0);
    // The one unavoidable string copy of the key (see WHAT JAVASCRIPT CANNOT WIPE).
    const address = privateKeyToAccount(keyHex.toString("latin1")).address;
    if (ks.address !== null && ks.address !== address.toLowerCase()) {
      throw new Refusal("refused: the decrypted key does not match the keystore's own address field");
    }
    if (address.toLowerCase() !== expect) {
      throw new Refusal(`refused: the decrypted key's address ${address} is not --expect`);
    }

    process.stdout.write(`${address}\n`);
    if (dryRun) {
      process.stderr.write("dry run: nothing pushed\n");
      return 0;
    }
    if (!vercelEnvAdd("TREASURY_PRIVATE_KEY", ["--sensitive"], keyHex)) {
      process.stderr.write("TREASURY_PRIVATE_KEY was not pushed; NEXT_PUBLIC_TREASURY_ADDRESS left alone\n");
      return 1;
    }
    keyHex.fill(0);
    if (!vercelEnvAdd("NEXT_PUBLIC_TREASURY_ADDRESS", ["--no-sensitive"], Buffer.from(address, "latin1"))) {
      return 1;
    }
    const problem = verifyPlacement();
    if (problem) {
      process.stderr.write(`pushed, but placement check FAILED: ${problem}. Fix it before redeploying ` +
        `(npx ${VERCEL_CLI} env rm <NAME> <environment> --scope ${VERCEL_SCOPE}).\n`);
      return 1;
    }
    process.stderr.write("verified: TREASURY_PRIVATE_KEY is a Production Secret, absent from Preview and Development\n");
    return 0;
  } finally {
    passphrase?.fill(0);
    key?.fill(0);
    keyHex?.fill(0);
    for (const sig of Object.keys(EXIT_FOR)) process.removeListener(sig, onSignal);
  }
}

main().then(
  (code) => {
    process.exitCode = code;
  },
  (err) => {
    if (err instanceof Refusal) {
      process.stderr.write(`treasury-push-key: ${err.message}\n`);
      process.exitCode = err.exitCode;
    } else {
      // An unexpected failure. Its message could carry library text, so only its class is shown.
      process.stderr.write(`treasury-push-key: failed (${err?.constructor?.name ?? "unknown error"})\n`);
      process.exitCode = 1;
    }
  },
);
