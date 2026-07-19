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

// One entry describes an expected outcome for a set of files matched by
// path_glob (relative to --content-path). See adversarial_manifest.json for
// the shipping schema and field semantics.
//
// Two orthogonal purposes served by the same entry shape:
//
//   1. Adversarial rejection (default): file is expected to be rejected by
//      the demo with `expectedExitCode`. Set expectedExitCode > 0.
//
//   2. Driver/hardware-conditional skip: if `skipOnGpu` is non-empty AND at
//      least one pattern matches the CURRENT GPU (--gpu-name at wrapper
//      startup), the test is GTEST_SKIP'd with `reason` and optional
//      `trackingBug` cited. If `skipScenarios` is non-empty, the skip is
//      restricted to those scenario names; empty means all scenarios.
//
// Both can coexist: an entry with expectedExitCode=1 AND skipOnGpu=["*GTX 1060*"]
// means "adversarial on most machines, but skip on GTX 1060 (probably a driver
// bug we're tracking separately)". Skip-on-gpu is checked BEFORE the demo is
// spawned so no cycles are wasted running a known-broken combination.
struct AdversarialEntry
{
    std::string pathGlob;                          // e.g. "public/fuzz/generated/gen_bitflip_off0[0-3]_*.zst"
    std::string reason;                            // Human-readable description of what's wrong with the file(s) (JSON key "reason")
    int expectedExitCode = 1;                      // Expected demo process exit code when NOT skipped (0 = don't check, non-zero = adversarial rejection)
    std::vector<std::string> skipOnGpu;            // Optional GPU-name glob patterns; if any match the current gpuName, wrapper skips the test
    std::vector<std::string> skipScenarios;        // Optional scenario names limiting the skip; empty = all scenarios (only meaningful when skipOnGpu non-empty)
    std::string trackingBug;                       // Optional reference for the skip (bug ID, TSG link, etc.) — printed in the skip diagnostic
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
