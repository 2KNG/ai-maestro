# Codex configuration write audit

Audit date: 2026-10-21. This is a static inspection of installed code, not a reproduction of configuration loss. No user configuration contents, credentials, live configuration changes, or service operations were needed.

## Installed build and evidence notation

Orca installed under `/home/u24c9g5090r64/.local/opt/orca/resources/app.asar.unpacked` reports version **1.4.232**, commit `49109f362f057cd2dc3d81cd378cb84d140c3dc5` in `out/package.json:5–6`.

References below are relative to `out/main/chunks/`. These are minified installed bundles: line numbers are original physical lines, and function symbols disambiguate multiple operations on one line. Names and offsets are build-specific.

| Alias | Installed filename |
| --- | --- |
| hooks | `managed-agent-hook-controls-CwMt99By.js` |
| homes | `codex-home-paths-BqK4A4GW.js` |
| fs | `fs-utils-DbiXACGC.js` |
| installer | `installer-utils-CHzcu3oC.js` |
| remote | `hook-service-BSr4PQ9c.js` |

`homes:1` defines the system home as `homedir()/.codex` (`b`) and the managed home as `<Orca user-data>/codex-runtime-home/home` (`x`, `S`). The latter is usually under `~/.config/orca` on Linux but honors Orca/XDG overrides. They are separate locations; a managed-home write is not automatically a system-home write.

## Confirmed configuration mutation paths

| Path / purpose | Evidence | Write behavior |
| --- | --- | --- |
| Runtime settings promoted into system `config.toml` | `hooks:242`, `Ms`, called by `js` / `Us`; target resolution `yo` at line 226 | Reads both homes and a baseline; merges selected changed settings and registrations. Resolves the system target, including symlinks. Ordinary path uses `fs.We` via export `i`: UTF-8 sibling temporary file followed by rename, with target mode supplied. No `.bak` in this path. **Existing WSL-path target uses direct `writeFileSync(path, text, "utf-8")` instead.** |
| System config mirrored into managed `config.toml` | `hooks:244`, `Js`, `Ks`, `Us` | `Js` uses UTF-8 temporary-file rename without `.bak`. `Ks` uses guarded publication (`fs.Ge`/`Z`) and first recovers a leftover `.orca-guarded` file. Missing/blank system source is treated separately rather than blindly mirrored over an existing managed file. |
| Managed daemon socket guard | `hooks:244`, `Ws`, `Gs`; transformation `yt` at line 18 | Adds the daemon-auto-start guard when needed and publishes through guarded write `fs.Ge`; not a system-wide configuration reset. |
| Project trust and hook approval additions, removals, moves | `hooks:218`, `da`, `ta`, `ga`, `la`, `ia`, `ha`; writer `$r` and resolver `ei` at line 185 | Operates on the passed TOML path, including managed/system/project candidates. Reads UTF-8; `Sa` removes a leading UTF-8 BOM. Resolves an existing symlink target, preserves existing mode, writes a sibling UTF-8 temporary file, replaces `<resolved-target>.bak`, then renames into place. `ia` refuses certain unsupported hook approval forms. |
| System-home hook approval promotion and cleanup | `hooks:245–249`, `Xc`, `Wc`, `Oc`, `Ac`, `Du`, `ll` | Cleanup of Orca-owned hooks can also rewrite system `hooks.json` and corresponding system `config.toml` trust entries. `ll` promotes matching user hook approvals into system TOML through `ta`. These are actual system-home writers, not merely readers of a managed copy. |
| WSL managed hook installation / stale trust removal | `hooks:254`, `Zd`, `Xd`, `Nc` | Uses Windows-form `configPath` / `tomlPath` and Linux-form command/trust paths. Writes hook script and JSON, removes stale trust, then installs approvals; fallback uses `ta` and the backed-up TOML writer. This is distinct from the direct WSL promotion write in `Ms`. |
| Remote Codex hook installation | `hooks:249`, `Vu` | Targets explicit remote `codexHomeDir`, or remote `~/.codex`; writes `hooks.json`, hook script, and TOML approval entries. Can defer approvals until config is seeded. Uses remote writers described below. |
| Legacy Orca status-hook config cleanup | `hooks:245–249`, `Jc`, `Qc`, `$c` | Removes only the marked legacy block in system `orca-agent-status.config.toml`; unlinks that legacy file if the remainder is blank, otherwise uses `ha`. This is not deletion of main `config.toml`. |

The promotion allowlist (`hooks:226`, `xo`, `So`, `Co`) includes `model`, `model_reasoning_effort`, `approval_policy`, `sandbox_mode`, four `tui` options, and `features.daemon_auto_start`. `Ms` additionally handles registration changes through `vs` / `ys`. This establishes intentional mutation capability, not evidence that an observed loss was caused by that path.

## Related home resources and persistence

- `homes:1`, `E` / `O` / `k`: mirrors `skills`, `hooks`, `plugins`, `plugin-state`, `profile-v2`, `themes`, `prompts`, and `AGENTS.md` from system into managed home. Normally creates symlinks; fallback removes a managed destination then recursively copies. `D` prefers a copy for `AGENTS.md`. `.orca-resource-copies/<name>.json` records ownership in UTF-8, mode `0600`. Stale recognized links/copies can be removed. The resource list does **not** include `config.toml`; config has the separate synchronization paths above. A symlink still exposes the same underlying resource to later consumers.
- `hooks:227`, `Fo`: writes managed `.orca-config-settings-baseline.json` directly in UTF-8, mode `0600`, without a backup or atomic replacement. `hooks:249`, `sl` similarly writes `.orca-hook-trust-provenance.json`. `hooks:245`, `hc` writes Orca's `trust-grant-ledger.json` directly; `dc` locates it beside the runtime-home directory.
- `hooks:221`, `Ya`, and `installer:4`, `we`: hook JSON replacement resolves symlinks, uses an exclusive sibling temporary file (`wx`), optionally preserves mode, replaces `.bak`, then renames. `installer:1`, `x` refuses a symlinked backup destination and publishes the backup through its own temporary file. Script writer `installer:4`, `Se` uses temporary replacement and executable mode, with an optional Windows locked-target fallback.
- `hooks:245`, `Vc`: keeps a first `hooks.json.pre-orca` copy under a `codex-real-home-hooks` directory for the supplied home; this is hook-specific, not a general config history.
- `remote:1`, `se`, `b`, `x`: SFTP text/JSON/script writers compare existing content, create a sibling temporary file, write UTF-8, chmod, and rename. `E` prefers the OpenSSH rename extension and falls back to ordinary SFTP rename (`le`). Atomic replacement semantics therefore depend on server capabilities. No `.bak` is made by these remote writers.

`fs:1`, `We` is an atomic-replacement helper, not a compare-and-swap: another writer can change the destination between read and rename. `Ge` / `Z` instead stages an old destination as `.orca-guarded`, verifies expected bytes, and publishes with a hard link that refuses an existing destination; recovery helpers restore staged content. This has different visibility and recovery behavior from a single rename. Neither helper shows `fsync` durability guarantees. `hooks:218`, `Ta` serializes operations inside this Orca process; it is not a cross-process lock honored by arbitrary external writers.

## ai-maestro boundary and conclusion

The pre-existing repository search found `install.sh:123` writing `~/.codex/AGENTS.md` by shell redirection, not `config.toml`. `skills/multi-agent-orchestrator/scripts/check_all_usage.py:1339` reads config for usage discovery. The new safe-config helper is a separate change and must be judged by its own tests; it is not evidence about the earlier incident.

The inspected Orca build can rewrite both managed and system Codex configuration. Most identified writers use temporary replacement; some preserve a single backup; the WSL promotion branch directly overwrites an existing file. UTF-8 is explicit in these writers, but the audit did not execute malformed-encoding cases or prove preservation of every comment, table, or concurrent edit.

No process trace, incident-time file history, or controlled reproduction was collected. Therefore this audit cannot attribute a particular wipe, encoding corruption, or lost setting to Orca, ai-maestro, Codex CLI, or another process. The inventory covers the configuration synchronization, trust, hook, and managed-resource code paths traced in these installed bundles; it is not an exhaustive audit of unrelated session/auth/cache writes or dynamically loaded plugins.

A guard around ai-maestro operations can validate inputs, preserve recoverable snapshots, refuse invalid replacements, and detect changes across those operations. It **cannot continuously prevent an unrelated process from rewriting `~/.codex/config.toml`**, and a lock only coordinates writers that honor it. A recovery snapshot is not proof of a writer's identity. Re-audit installed bundles after Orca upgrades; if causality is needed, collect narrowly scoped write-attribution evidence and hashes during a reproduction without logging configuration secrets.
