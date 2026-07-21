/**
 * Copyright (c) Microsoft. All rights reserved.
 * This code is licensed under the MIT License (MIT).
 * THIS CODE IS PROVIDED *AS IS* WITHOUT WARRANTY OF
 * ANY KIND, EITHER EXPRESS OR IMPLIED, INCLUDING ANY
 * IMPLIED WARRANTIES OF FITNESS FOR A PARTICULAR
 * PURPOSE, MERCHANTABILITY, OR NON-INFRINGEMENT.
 */

// Adversarial manifest: maps known-adversarial fuzz files to their expected
// demo exit code. When a matching file is exercised by the wrapper, the
// correctness test expects exit_code == manifest value rather than success.
//
// Perf tests do NOT consult the manifest; they skip fuzz content by path (see
// IsFuzzContent in zstdgpu_ci_tests.cpp).
//
// If the manifest is not loaded (--adversarial-manifest not passed OR file
// missing), the wrapper falls back to legacy behavior: every file expected to
// succeed. This is intentionally additive — no test starts failing just
// because the manifest isn't wired up yet.

#pragma once

#include <filesystem>
#include <string>
#include <vector>

// Manifest entry: matches files by path_glob (relative to --content-path) and
// declares either an adversarial rejection (expectedExitCode > 0) or a
// GPU-conditional skip (skipOnGpu non-empty), or both. Skip-on-gpu is checked
// before the demo is spawned; expected_exit_code is enforced against the
// resulting process exit code. See adversarial_manifest.json for the schema.
struct AdversarialEntry
{
    std::string pathGlob;                          // e.g. "public/fuzz/generated/gen_bitflip_off0[0-3]_*.zst"
    std::string reason;                            // Human-readable description of what's wrong with the file(s)
    int expectedExitCode = 1;                      // Expected demo exit code when NOT skipped (0 = don't check)
    std::vector<std::string> skipOnGpu;            // GPU-name glob patterns; match triggers GTEST_SKIP
    std::vector<std::string> skipScenarios;        // If non-empty, restricts the skip to these scenarios
    std::string trackingBug;                       // Reference for the skip (bug ID, TSG link, etc.)
};

// Loads and matches the manifest. Loaded once at startup, then read-only from
// test threads. Thread-safe for concurrent Match() calls after LoadFromFile()
// returns.
class AdversarialManifest
{
public:
    // Loads entries from a JSON file on disk. Returns false if the file is
    // missing, malformed, or has an unsupported schema_version. On failure,
    // writes a diagnostic message to errorOut and leaves the manifest empty
    // (Loaded() returns false; Match() always returns nullptr).
    bool LoadFromFile(const std::filesystem::path& jsonPath, std::string& errorOut);

    // Returns the matching entry for the given file, or nullptr if no match.
    // Matching is on the path relative to contentPath, normalized to forward
    // slashes. If contentPath is empty, matches on the leaf filename only.
    // O(N) scan of entries; N is expected to be small (~12 entries today).
    const AdversarialEntry* Match(const std::string& zstFullPath,
                                  const std::filesystem::path& contentPath) const;

    size_t Size() const { return m_entries.size(); }
    bool Loaded() const { return m_loaded; }
    const std::filesystem::path& SourcePath() const { return m_sourcePath; }

    // Startup coverage self-check. Returns how many of `files` match at least
    // one manifest entry. The wrapper uses this to fail loud when a loaded
    // manifest matches nothing (an inert manifest is otherwise indistinguishable
    // from a working one and silently disables the adversarial-rejection checks).
    size_t CountCoverage(const std::vector<std::string>& files,
                         const std::filesystem::path& contentPath) const;

    // Returns true if `entry` declares a skip that applies to (gpuName, scenarioName).
    // Preconditions: entry.skipOnGpu non-empty AND gpuName non-empty. If entry
    // .skipScenarios is empty, all scenarios are in scope; otherwise only the
    // listed names are. GPU-name matching uses the same glob syntax as pathGlob
    // (`*` wildcards and `[a-b]` ranges), unanchored — a pattern like
    // "*GTX 1060*" matches anywhere in the adapter string.
    static bool EntryTargetsSkip(const AdversarialEntry& entry,
                                  const std::string& gpuName,
                                  const std::string& scenarioName);

private:
    std::vector<AdversarialEntry> m_entries;
    std::filesystem::path m_sourcePath;
    bool m_loaded = false;
};
