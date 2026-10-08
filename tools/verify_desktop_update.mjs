#!/usr/bin/env node
/**
 * Desktop updater release gate.
 *
 * A sha256 comparison proves the copies are identical, but not that the signature
 * matches the public key pinned in tauri.conf.json. Ship a package signed with the
 * wrong key (or a stale signature) and every client's auto-update fails silently.
 * This script verifies exactly what clients will download:
 *
 *   1. manifest version matches the version in tauri.conf.json
 *   2. manifest signature verifies against the pinned public key
 *   3. the downloaded installer bytes verify against that signature
 *   4. manifest url / signature still describe the artefact actually served
 *
 * Signature layout is minisign with Tauri's framing:
 *   - the `.sig` file (and the manifest `signature` field) is base64 of a minisign
 *     signature file: untrusted comment / base64 blob / trusted comment / base64 blob
 *   - the blob is alg(2) || key_id(8) || ed25519_signature(64)
 *   - alg "ED" means the signed message is BLAKE2b-512(data), not data itself
 *   - the second signature covers signature(64) || trusted_comment (without the prefix)
 *
 * Usage:
 *   node tools/verify_desktop_update.mjs            # verify the published channel
 *   node tools/verify_desktop_update.mjs --local    # also verify the local build
 *
 * No dependencies: uses only node:crypto.
 */

import { createHash, createPublicKey, verify as cryptoVerify } from "node:crypto";
import { readFile, readdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const TAURI_CONF = path.join(REPO_ROOT, "frontend", "src-tauri", "tauri.conf.json");
const NSIS_DIR = path.join(REPO_ROOT, "frontend", "src-tauri", "target", "release", "bundle", "nsis");

const failures = [];

function check(label, ok, detail = "") {
  console.log(`  [${ok ? "PASS" : "FAIL"}] ${label}${detail ? ` -> ${detail}` : ""}`);
  if (!ok) failures.push(label);
}

/** Rebuild a raw Ed25519 public key into a DER SPKI KeyObject (node:crypto needs DER). */
function ed25519KeyFromRaw(raw) {
  const spkiPrefix = Buffer.from("302a300506032b6570032100", "hex");
  return createPublicKey({ key: Buffer.concat([spkiPrefix, raw]), format: "der", type: "spki" });
}

function decodePublicKey(pubkeyBase64) {
  // The config value is base64 of a two-line minisign public key file.
  const text = Buffer.from(pubkeyBase64, "base64").toString("utf8");
  const lines = text.split("\n").map((line) => line.trim()).filter(Boolean);
  const blob = Buffer.from(lines[lines.length - 1], "base64");
  if (blob.length !== 42) throw new Error(`unexpected public key length ${blob.length}`);
  return { alg: blob.subarray(0, 2).toString("latin1"), keyId: blob.subarray(2, 10), key: ed25519KeyFromRaw(blob.subarray(10, 42)) };
}

function decodeSignature(signatureBase64) {
  const text = Buffer.from(signatureBase64, "base64").toString("utf8");
  const lines = text.split("\n").map((line) => line.trim()).filter(Boolean);
  const blob = Buffer.from(lines[1], "base64");
  if (blob.length !== 74) throw new Error(`unexpected signature length ${blob.length}`);
  const trustedPrefix = "trusted comment: ";
  if (!lines[2].startsWith(trustedPrefix)) throw new Error("trusted comment line missing");
  return {
    alg: blob.subarray(0, 2).toString("latin1"),
    keyId: blob.subarray(2, 10),
    signature: blob.subarray(10, 74),
    blob,
    trustedComment: lines[2].slice(trustedPrefix.length),
    globalSignature: Buffer.from(lines[3], "base64"),
  };
}

async function fetchWithUa(url) {
  const response = await fetch(url, { headers: { "user-agent": "Mozilla/5.0 (update-verifier)" } });
  if (!response.ok) throw new Error(`${url} -> HTTP ${response.status}`);
  return Buffer.from(await response.arrayBuffer());
}

function verifyArtifact(label, bytes, signatureBase64, pubkey, expectedVersion) {
  console.log(`\n${label}`);
  const sig = decodeSignature(signatureBase64);

  const mode = sig.alg === "ED" ? "BLAKE2b-512 预哈希" : "原始数据";
  check("签名算法标识可识别", sig.alg === "ED" || sig.alg === "Ed", `${sig.alg} (${mode})`);
  check("密钥 ID 与公钥一致", sig.keyId.equals(pubkey.keyId), pubkey.keyId.toString("hex").toUpperCase());
  check("安装包大小合理", bytes.length > 1_000_000, `${bytes.length} bytes`);

  const message = sig.alg === "ED" ? createHash("blake2b512").update(bytes).digest() : bytes;
  check("文件签名有效", cryptoVerify(null, message, pubkey.key, sig.signature), `Ed25519（${mode}）`);
  check(
    "可信注释签名有效",
    cryptoVerify(
      null,
      Buffer.concat([sig.signature, Buffer.from(sig.trustedComment, "utf8")]),
      pubkey.key,
      sig.globalSignature,
    ),
  );
  check("可信注释含文件名与时间戳", /file:.+timestamp:|timestamp:.+file:/.test(sig.trustedComment), sig.trustedComment);
  if (expectedVersion) {
    check(`签名覆盖的版本为 ${expectedVersion}`, sig.trustedComment.includes(expectedVersion));
  }
}

async function main() {
  const alsoLocal = process.argv.includes("--local");

  const conf = JSON.parse(await readFile(TAURI_CONF, "utf8"));
  const version = conf.version;
  const endpoint = conf.plugins.updater.endpoints[0];
  const base = endpoint.replace(/\/[^/]*$/, "");
  const pubkey = decodePublicKey(conf.plugins.updater.pubkey);
  console.log(`校验版本 ${version}，更新端点 ${endpoint}`);
  console.log(`公钥算法 ${pubkey.alg}，密钥 ID ${pubkey.keyId.toString("hex").toUpperCase()}`);

  console.log("\n更新清单");
  const manifest = JSON.parse((await fetchWithUa(endpoint)).toString("utf8"));
  const platform = manifest.platforms?.["windows-x86_64"] ?? {};
  check(`清单版本为 ${version}`, manifest.version === version, manifest.version);
  check("清单含 signature 与 url", Boolean(platform.signature && platform.url), platform.url);

  console.log("\n下载客户端实际拉取的安装包");
  const published = await fetchWithUa(platform.url);
  verifyArtifact("公网安装包", published, platform.signature, pubkey, version);

  if (alsoLocal) {
    const entries = await readdir(NSIS_DIR).catch(() => []);
    const localName = entries.find((name) => name.endsWith(`_${version}_x64-setup.exe`));
    if (!localName) {
      check(`本地存在 ${version} 安装包`, false, NSIS_DIR);
    } else {
      const localBytes = await readFile(path.join(NSIS_DIR, localName));
      const localSig = (await readFile(path.join(NSIS_DIR, `${localName}.sig`), "utf8")).trim();
      verifyArtifact(`本地产物 ${localName}`, localBytes, localSig, pubkey, version);
      console.log("\n本地产物与公网副本");
      check("字节完全一致", localBytes.equals(published), `${localBytes.length} bytes`);
    }
  }

  console.log();
  if (failures.length) {
    console.log(`结果：${failures.length} 项失败 -> ${failures.join("; ")}`);
    process.exitCode = 1;
    return;
  }
  console.log("结果：全部通过，更新器可接受该安装包");
}

main().catch((error) => {
  console.error(`校验中断：${error.message}`);
  process.exitCode = 1;
});
