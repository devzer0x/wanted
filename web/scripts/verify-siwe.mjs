#!/usr/bin/env node
// Regression test for the SIWE domain binding. Run: `npm run verify:siwe` (from web/).
//
// This exists because of a real vulnerability that shipped into a draft of /api/auth/verify: the
// route reconstructed the SIWE message from the request's `Host` header and then compared the
// parsed domain against that same header. Both sides came from the caller, so the comparison
// always passed and EIP-4361's domain binding did nothing. The attack it enabled:
//
//   1. attacker requests a nonce from us for the victim's address (public route, takes an address)
//   2. attacker has the victim sign a SIWE message on evil.example carrying that nonce
//   3. attacker POSTs the signature to /api/auth/verify with `Host: evil.example`
//   4. we rebuild a message with domain evil.example, it matches, the signature is valid, and the
//      attacker holds a session cookie for the victim's address
//
// The fix pins domain/uri to the deployment's configured origin (app/api/_lib/siweDomain.ts).
// Case 3 below is the regression witness: it asserts that the phished signature WOULD have been
// accepted under a Host-derived domain, so if anyone reintroduces that pattern, case 2 fails and
// this file explains exactly why.
//
// No test runner and no new dependency: it compiles the real src/lib/auth/siwe.ts with the
// project's own tsc and exercises it with real secp256k1 signatures from viem.

import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = join(fileURLToPath(new URL(".", import.meta.url)), "..");

// Compile into web/ so that `viem` resolves from the project's own node_modules.
const outDir = mkdtempSync(join(webRoot, ".siwe-verify-"));
let failures = 0;

try {
  execFileSync(
    "npx",
    ["tsc", "src/lib/auth/siwe.ts", "--outDir", outDir, "--module", "esnext",
     "--target", "es2020", "--moduleResolution", "bundler", "--skipLibCheck"],
    { cwd: webRoot, stdio: "inherit" },
  );

  const { buildSiweMessage, verifySiwe } = await import(join(outDir, "siwe.js"));
  const { privateKeyToAccount, generatePrivateKey } = await import("viem/accounts");

  const account = privateKeyToAccount(generatePrivateKey());
  const nonce = "abcdef1234567890";
  const base = {
    address: account.address,
    nonce,
    chainId: 4663,
    issuedAt: new Date().toISOString(),
    expirationTime: new Date(Date.now() + 600_000).toISOString(),
  };

  const PINNED = { domain: "wanted.example", uri: "https://wanted.example" };
  const EVIL = { domain: "evil.example", uri: "https://evil.example" };

  const honest = buildSiweMessage({ ...base, ...PINNED });
  const phished = buildSiweMessage({ ...base, ...EVIL });
  const sigHonest = await account.signMessage({ message: honest });
  const sigPhished = await account.signMessage({ message: phished });

  const check = (name, ok) => {
    if (ok) console.log("  PASS  " + name);
    else { failures += 1; console.log("  FAIL  " + name); }
  };

  check(
    "an honest signature verifies against the pinned domain",
    (await verifySiwe({ message: honest, signature: sigHonest, expectedNonce: nonce,
      expectedDomain: PINNED.domain, expectedAddress: account.address })).ok === true,
  );

  const replay = await verifySiwe({ message: phished, signature: sigPhished, expectedNonce: nonce,
    expectedDomain: PINNED.domain, expectedAddress: account.address });
  check(
    "a signature phished on another origin is REJECTED against the pinned domain",
    replay.ok === false && replay.error === "domain_mismatch",
  );

  check(
    "regression witness: that same signature WOULD pass with a Host-derived domain",
    (await verifySiwe({ message: phished, signature: sigPhished, expectedNonce: nonce,
      expectedDomain: EVIL.domain, expectedAddress: account.address })).ok === true,
  );

  check(
    "a tampered nonce fails",
    (await verifySiwe({ message: honest.replace(nonce, "ffffffffffffffff"), signature: sigHonest,
      expectedNonce: "ffffffffffffffff", expectedDomain: PINNED.domain,
      expectedAddress: account.address })).ok === false,
  );

  // The database round-trip. This is the bug that took sign-in down completely: /nonce builds the
  // message from an ISO string, Postgres hands the same instant back as "+00:00", and /verify
  // rebuilds from THAT. Same instant, different bytes, every signature invalid. Both halves look
  // correct in isolation, which is why only a round-trip test finds it.
  const pgShaped = {
    ...base,
    issuedAt: base.issuedAt.replace("Z", "+00:00").replace("T", " "),
    expirationTime: base.expirationTime.replace("Z", "+00:00").replace("T", " "),
  };
  check(
    "a Postgres-shaped timestamp builds byte-identical bytes to an ISO one",
    buildSiweMessage({ ...pgShaped, ...PINNED }) === honest,
  );
  check(
    "and a signature made against the ISO form still verifies after the DB round-trip",
    (await verifySiwe({ message: buildSiweMessage({ ...pgShaped, ...PINNED }), signature: sigHonest,
      expectedNonce: nonce, expectedDomain: PINNED.domain, expectedAddress: account.address })).ok === true,
  );

  const other = privateKeyToAccount(generatePrivateKey());
  check(
    "a signature from a different key cannot claim the address",
    (await verifySiwe({ message: honest, signature: await other.signMessage({ message: honest }),
      expectedNonce: nonce, expectedDomain: PINNED.domain,
      expectedAddress: account.address })).ok === false,
  );
} finally {
  rmSync(outDir, { recursive: true, force: true });
}

console.log(failures === 0 ? "\n  SIWE domain binding holds.\n" : `\n  ${failures} FAILED\n`);
process.exit(failures === 0 ? 0 : 1);
