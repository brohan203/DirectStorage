/**
 * Copyright (c) Microsoft. All rights reserved.
 * This code is licensed under the MIT License (MIT).
 * THIS CODE IS PROVIDED *AS IS* WITHOUT WARRANTY OF
 * ANY KIND, EITHER EXPRESS OR IMPLIED, INCLUDING ANY
 * IMPLIED WARRANTIES OF FITNESS FOR A PARTICULAR
 * PURPOSE, MERCHANTABILITY, OR NON-INFRINGEMENT.
 */

// Test definitions and demo runner for the Zstd GPU CI tests.
//
// Parameterized test suite (ZstdGpuDemoTests) instantiated once per .zst file
// under the content path. The scenario set is the "for all data" correctness
// matrix from the DS/ATG spec (6 baseline scenarios per file, plus 2 GBV
// variants for small files) and one performance scenario.
//
//   Correctness (ASSERT — hard fail) — runs on every file:
//     - GpuCheck             : --chk-gpu
//     - GpuCheckSeq          : --chk-gpu --seq-cnt
//     - D3D12DebugLayer      : --chk-gpu --d3d-dbg                          [ARM: skipped]
//     - D3D12DebugLayerSeq   : --chk-gpu --d3d-dbg --seq-cnt                [ARM: skipped]
//     - SimulationCheck      : --chk-gpu --chk-cpu --sim-gpu
//     - SimulationCheckSeq   : --chk-gpu --chk-cpu --sim-gpu --seq-cnt
//
//   Correctness (ASSERT) — runs only on files <= --gbv-max-mb (default 4 MB):
//     - Gbv                  : --chk-gpu --d3d-dbg --d3d-gbv               [ARM: skipped]
//     - GbvSeq               : --chk-gpu --d3d-dbg --d3d-gbv --seq-cnt     [ARM: skipped]
//
//   Performance (EXPECT — soft fail, also verifies CSV output was written) —
//     skips files under 'fuzz/' AND files smaller than --perf-min-mb (default 4 MB):
//     - PerStageTiming       : --prf-lvl 2 --d3d-gfx --seq-cnt  → results/stages_<stem>.csv

#include "zstdgpu_ci_tests.h"
#include <gtest/gtest.h>
#include <array>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sstream>
#include <thread>
#include <Windows.h>

// Internal types + forward declarations
//
// These are used only inside this translation unit; keeping them out of the
// header limits the wrapper's public surface to the two things main.cpp
// actually needs (TestConfig, DiscoverZstFiles).

namespace
{
    // Captures the outcome of a single demo process invocation.
    struct DemoResult
    {
        int exitCode = -1;        // Process exit code (0 = success)
        std::string stdOut;       // Captured stdout + stderr
        std::string launchError;  // Error message if the process failed to launch
        std::string commandLine;  // The exact command line that was executed
        bool timedOut = false;    // True if the process was killed due to timeout
    };

    DemoResult RunDemo(
        const std::string& demoPath,
        const std::vector<std::string>& args,
        int timeoutSeconds);

    std::vector<std::string> BuildCorrectnessArgs(
        const std::string& zstFile,
        const std::vector<std::string>& scenarioFlags);

    std::vector<std::string> BuildPerformanceArgs(
        const std::string& zstFile,
        int profilingLevel,
        int runCount,
        const std::string& csvOutputPath,
        const std::vector<std::string>& extraFlags);
}

// Helpers

// Returns the list of .zst files to parameterize over.
// GTest evaluates this lazily when the test suite is instantiated (after main
// has parsed CLI args, validated inputs, and cached the discovered file list
// in TestConfig). We just return the cached list here — the actual filesystem
// walk happened once, up front, in main().
static std::vector<std::string> GetTestFiles()
{
    return g_testConfig.discoveredFiles;
}

// Converts a full file path to a valid GTest parameter name.
// GTest names must be alphanumeric + underscore, no leading digits, AND UNIQUE
// across the full INSTANTIATE_TEST_SUITE_P set. Using just the filename stem
// collides when the same leaf name appears across different subdirectories
// (e.g. firefly_albedo.DDS.zst exists under BC1/, BC1mip0/, block4K_*, etc.),
// causing a fatal gtest assertion at startup. Use the path relative to
// --content-path so different folders produce different names.
static std::string SanitizeTestName(const testing::TestParamInfo<std::string>& info)
{
    std::filesystem::path full(info.param);
    std::filesystem::path rel;
    if (!g_testConfig.contentPath.empty())
    {
        std::error_code ec;
        rel = std::filesystem::relative(full, g_testConfig.contentPath, ec);
        if (ec || rel.empty() || rel.string().rfind("..", 0) == 0)
        {
            rel = full.filename();  // fallback: out-of-tree, just use leaf
        }
    }
    else
    {
        rel = full.filename();
    }

    // Drop the trailing .zst extension for readability; everything else stays.
    std::string name = rel.string();
    const std::string ext = ".zst";
    if (name.size() >= ext.size() &&
        name.compare(name.size() - ext.size(), ext.size(), ext) == 0)
    {
        name.resize(name.size() - ext.size());
    }

    std::string result;
    result.reserve(name.size());
    for (char c : name)
    {
        result += std::isalnum(static_cast<unsigned char>(c)) ? c : '_';
    }
    if (!result.empty() && std::isdigit(static_cast<unsigned char>(result[0])))
    {
        result = "_" + result;
    }
    return result.empty() ? "Unknown" : result;
}

// Appends per-test output to the consolidated log file (--log-file).
static void WriteToLogFile(const std::string& zstFile, const DemoResult& result)
{
    if (g_testConfig.logFile.empty())
        return;

    std::ofstream log(g_testConfig.logFile, std::ios::app);
    if (!log)
    {
        std::cerr << "Warning: could not open --log-file '"
                  << g_testConfig.logFile << "' for append.\n";
        return;
    }
    auto* testInfo = ::testing::UnitTest::GetInstance()->current_test_info();
    log << "=== " << testInfo->test_suite_name() << "." << testInfo->name() << " ===\n";
    log << "File: " << zstFile << "\n";
    log << "Exit code: " << result.exitCode << "\n";
    log << result.stdOut << "\n";
}

// Test runners

// Returns true if the .zst file lives under a directory named "fuzz". Fuzzing
// content is a mix of clean and corrupt inputs that exercise varying code
// paths, so its timing isn't meaningful performance data — perf tests skip it.
// This is a pure path check, independent of the manifest.
static bool IsFuzzContent(const std::string& zstFile)
{
    for (const auto& part : std::filesystem::path(zstFile))
    {
        if (part == "fuzz")
            return true;
    }
    return false;
}

// Returns true if the .zst file is small enough for the Gbv/GbvSeq scenarios
// to run against it. GBV multiplies per-dispatch cost 20-50x on some drivers
// so it's only affordable on small inputs; large concat batches would blow
// the wrapper timeout. Threshold is --gbv-max-mb (default 4 MB).
//
// On a stat error (permissions, race, network path glitch) returns false —
// safer to skip GBV than to blindly run it on a possibly-huge file we can't
// size.
static bool IsSmallForGbv(const std::string& zstFile)
{
    std::error_code ec;
    auto size = std::filesystem::file_size(zstFile, ec);
    if (ec) return false;
    return size <= static_cast<uintmax_t>(g_testConfig.gbvMaxMB) * 1024ULL * 1024ULL;
}

// Returns true if the .zst file is too small for perf tests to produce
// representative timing data. Individually-compressed textures (typically
// under a few MB) don't fill enough of the GPU pipeline to expose throughput
// characteristics. Threshold is --perf-min-mb (default 4 MB); set to 0 to
// disable this skip.
//
// On a stat error, returns false — we err toward running perf when we can't
// tell the size, so the failure surfaces in logs instead of silently skipping.
static bool IsSmallForPerf(const std::string& zstFile)
{
    if (g_testConfig.perfMinMB <= 0) return false;
    std::error_code ec;
    auto size = std::filesystem::file_size(zstFile, ec);
    if (ec) return false;
    return size < static_cast<uintmax_t>(g_testConfig.perfMinMB) * 1024ULL * 1024ULL;
}

// Returns the name of the currently-running test (e.g. "SimulationCheck").
// Used to feed AdversarialManifest::EntryTargetsSkip for scenario-scoped skips.
static std::string CurrentScenarioName()
{
    if (auto* info = ::testing::UnitTest::GetInstance()->current_test_info())
        return info->name();
    return "";
}

// Emits GTEST_SKIP for a manifest-declared GPU/scenario skip. Composes a
// diagnostic that includes the reason and (if present) the tracking bug so
// operators can find the owner without opening the manifest.
static void SkipForManifestGpuTarget(const AdversarialEntry& entry)
{
    std::string msg = "Skipped on GPU '" + g_testConfig.gpuName +
                      "' per adversarial manifest. Reason: " + entry.reason;
    if (!entry.trackingBug.empty())
    {
        msg += " (Tracking: " + entry.trackingBug + ")";
    }
    GTEST_SKIP() << msg;
}

// Run a correctness scenario. First consults the manifest for a GPU-conditional
// skip; if none applies, spawns zstdgpu_demo.exe and asserts on the outcome
// (either "exit == expected_exit_code" for adversarial entries, or exit == 0
// for legacy/non-matched entries).
//
// main() has already validated the demo path exists, so we don't re-check here.
// stdout is printed via the unconditional [DEMO OUT] block below and does NOT
// appear a second time in the ASSERT_EQ failure message — that would duplicate
// the same text in the log.
static void RunCorrectnessTest(const std::string& zstFile, const std::vector<std::string>& scenarioFlags)
{
    // Manifest lookup happens once. Two orthogonal checks:
    //   1. skip_on_gpu: skip the test entirely before spawning the demo
    //   2. expected_exit_code: score the demo's rejection (or lack thereof)
    const AdversarialEntry* entry =
        g_testConfig.adversarialManifest.Match(zstFile, g_testConfig.contentPath);

    if (entry && AdversarialManifest::EntryTargetsSkip(*entry, g_testConfig.gpuName, CurrentScenarioName()))
    {
        SkipForManifestGpuTarget(*entry);
        return;
    }

    auto args = BuildCorrectnessArgs(zstFile, scenarioFlags);
    auto result = RunDemo(g_testConfig.demoPath, args, g_testConfig.timeoutSeconds);

    // Write to log file before assertions so logs are captured even if an ASSERT aborts early.
    WriteToLogFile(zstFile, result);

    // Log the output regardless of pass/fail. This IS the demo stdout capture —
    // the assertion messages below intentionally do NOT reprint result.stdOut.
    std::cout << "[DEMO CMD] " << result.commandLine << "\n";
    if (!result.stdOut.empty())
    {
        std::cout << "[DEMO OUT] " << result.stdOut << "\n";
    }

    ASSERT_FALSE(result.timedOut)
        << "Demo process timed out after " << g_testConfig.timeoutSeconds << " seconds.\n"
        << "Command: " << result.commandLine;

    ASSERT_TRUE(result.launchError.empty())
        << "Failed to launch demo: " << result.launchError << "\n"
        << "Command: " << result.commandLine;

    // Adversarial path: manifest entry matched AND declares a non-zero expected
    // exit code. Score against it.
    if (entry && entry->expectedExitCode > 0)
    {
        if (result.exitCode != entry->expectedExitCode)
        {
            ADD_FAILURE()
                << "Adversarial file expected to be rejected with exit code "
                << entry->expectedExitCode << " but demo returned " << result.exitCode << ".\n"
                << "File: " << zstFile << "\n"
                << "Reason: " << entry->reason << "\n"
                << "Command: " << result.commandLine
                << "  (stdout already printed above as [DEMO OUT])";
        }
        return;
    }

    // Legacy path: no manifest match, OR match with expected_exit_code == 0
    // (i.e. entry existed only to declare a GPU-conditional skip that didn't
    // apply on this GPU). Expect the demo to succeed.
    ASSERT_EQ(result.exitCode, 0)
        << "Demo process returned non-zero exit code: " << result.exitCode << "\n"
        << "Command: " << result.commandLine
        << "  (stdout already printed above as [DEMO OUT])";
}

// Run a performance scenario. Spawns zstdgpu_demo.exe with profiling flags and requests CSV output. Uses EXPECT (not ASSERT) to verify the demo executed successfully and produced CSV output.
// main() has already validated the demo path exists.
static void RunPerformanceTest(const std::string& zstFile, int profilingLevel,
                                const std::vector<std::string>& extraFlags)
{
    // Fuzzing content mixes clean and corrupt inputs with varying code paths,
    // so its timing isn't meaningful perf data — skip it before running the demo.
    if (IsFuzzContent(zstFile))
    {
        GTEST_SKIP()
            << "Perf test skipped: file is fuzzing content (under a 'fuzz' directory).\n"
            << "File: " << zstFile;
        return;
    }

    // Skip individually-compressed textures: too small to fill the GPU pipeline
    // enough to produce representative throughput numbers. Threshold is
    // --perf-min-mb.
    if (IsSmallForPerf(zstFile))
    {
        GTEST_SKIP()
            << "Perf test skipped: file is under --perf-min-mb ("
            << g_testConfig.perfMinMB << " MB) — not representative for throughput.\n"
            << "File: " << zstFile;
        return;
    }

    // Build CSV output path. Only one perf scenario exists today (PerStageTiming),
    // so the "throughput_/stages_" prefix distinction from earlier is no longer
    // needed — plain stages_<stem>.csv is enough.
    std::string stem = std::filesystem::path(zstFile).stem().string();
    std::filesystem::path resultsDir = std::filesystem::path(g_testConfig.logDir) / "results";
    if (!std::filesystem::exists(resultsDir))
    {
        std::filesystem::create_directories(resultsDir);
    }
    std::string csvPath = (resultsDir / ("stages_" + stem + ".csv")).string();

    auto args = BuildPerformanceArgs(zstFile, profilingLevel, g_testConfig.runCount, csvPath, extraFlags);
    auto result = RunDemo(g_testConfig.demoPath, args, g_testConfig.timeoutSeconds);

    // Write to log file before assertions so logs are captured even if a check fails.
    WriteToLogFile(zstFile, result);

    // Log the output regardless of pass/fail. This IS the demo stdout capture —
    // the assertion messages below intentionally do NOT reprint result.stdOut.
    std::cout << "[DEMO CMD] " << result.commandLine << "\n";
    if (!result.stdOut.empty())
    {
        std::cout << "[DEMO OUT] " << result.stdOut << "\n";
    }

    EXPECT_FALSE(result.timedOut)
        << "Demo process timed out after " << g_testConfig.timeoutSeconds << " seconds.\n"
        << "Command: " << result.commandLine;

    EXPECT_TRUE(result.launchError.empty())
        << "Failed to launch demo: " << result.launchError << "\n"
        << "Command: " << result.commandLine;

    // Fuzz content and undersized files were already skipped above, so any file
    // reaching here is expected to decode successfully AND produce a CSV.
    EXPECT_EQ(result.exitCode, 0)
        << "Demo process returned non-zero exit code: " << result.exitCode << "\n"
        << "Command: " << result.commandLine
        << "  (stdout already printed above as [DEMO OUT])";

    EXPECT_TRUE(std::filesystem::exists(csvPath)) << "CSV not created: " << csvPath;

    if (std::filesystem::exists(csvPath))
    {
        std::cout << "[PERF CSV] Written to: " << csvPath << "\n";
    }
}

// Test fixture and test cases

// Test fixture parameterized over .zst file paths (spec: ZstdGpuDemoTests).
// Both correctness and performance tests share this fixture — correctness tests
// use ASSERT (hard fail), performance tests use EXPECT (soft fail).
class ZstdGpuDemoTests : public ::testing::TestWithParam<std::string>
{
};

// --- Correctness tests — "for all data" matrix ---

// Baseline GPU correctness check without debug layer or CPU-sim overhead.
// Cheapest correctness signal per file.
TEST_P(ZstdGpuDemoTests, GpuCheck)
{
    RunCorrectnessTest(GetParam(), {"--chk-gpu"});
}

// Same as GpuCheck but forces single-submission mode via --seq-cnt (implies
// --blk-cnt — pre-scanned block counts feed SetupBlockInfoConstants).
TEST_P(ZstdGpuDemoTests, GpuCheckSeq)
{
    RunCorrectnessTest(GetParam(), {"--chk-gpu", "--seq-cnt"});
}

TEST_P(ZstdGpuDemoTests, D3D12DebugLayer)
{
#if defined(_M_ARM) || defined(_M_ARM64) || defined(_M_ARM64EC)
    GTEST_SKIP() << "D3D12 debug layer tests are skipped on ARM platforms.";
#else
    RunCorrectnessTest(GetParam(), {"--chk-gpu", "--d3d-dbg"});
#endif
}

TEST_P(ZstdGpuDemoTests, D3D12DebugLayerSeq)
{
#if defined(_M_ARM) || defined(_M_ARM64) || defined(_M_ARM64EC)
    GTEST_SKIP() << "D3D12 debug layer tests are skipped on ARM platforms.";
#else
    RunCorrectnessTest(GetParam(), {"--chk-gpu", "--d3d-dbg", "--seq-cnt"});
#endif
}

TEST_P(ZstdGpuDemoTests, SimulationCheck)
{
    RunCorrectnessTest(GetParam(), {"--chk-gpu", "--chk-cpu", "--sim-gpu"});
}

TEST_P(ZstdGpuDemoTests, SimulationCheckSeq)
{
    RunCorrectnessTest(GetParam(), {"--chk-gpu", "--chk-cpu", "--sim-gpu", "--seq-cnt"});
}

// --- Correctness tests — GBV, small-file only ---

// GBV ("GPU-Based Validation") is expensive; it multiplies per-dispatch cost
// dramatically on some drivers. Only run on files <= --gbv-max-mb so the
// wrapper stays inside its timeout on large concat batches.
TEST_P(ZstdGpuDemoTests, Gbv)
{
#if defined(_M_ARM) || defined(_M_ARM64) || defined(_M_ARM64EC)
    GTEST_SKIP() << "D3D12 debug layer tests are skipped on ARM platforms.";
#else
    if (!IsSmallForGbv(GetParam()))
    {
        GTEST_SKIP() << "GBV skipped: file exceeds --gbv-max-mb ("
                     << g_testConfig.gbvMaxMB << " MB).";
        return;
    }
    // Passing --d3d-dbg explicitly matches the ATG spec even though --d3d-gbv
    // implies it — defensive against a future demo change that decouples them.
    RunCorrectnessTest(GetParam(), {"--chk-gpu", "--d3d-dbg", "--d3d-gbv"});
#endif
}

TEST_P(ZstdGpuDemoTests, GbvSeq)
{
#if defined(_M_ARM) || defined(_M_ARM64) || defined(_M_ARM64EC)
    GTEST_SKIP() << "D3D12 debug layer tests are skipped on ARM platforms.";
#else
    if (!IsSmallForGbv(GetParam()))
    {
        GTEST_SKIP() << "GBV skipped: file exceeds --gbv-max-mb ("
                     << g_testConfig.gbvMaxMB << " MB).";
        return;
    }
    RunCorrectnessTest(GetParam(), {"--chk-gpu", "--d3d-dbg", "--d3d-gbv", "--seq-cnt"});
#endif
}

// --- Performance tests ---

// Detailed per-pass timings via --prf-lvl 2, on the DIRECT queue (--d3d-gfx)
// in single-submission mode (--seq-cnt). Skips fuzz content and files smaller
// than --perf-min-mb (individually-compressed textures are too small to be
// representative of end-to-end throughput).
TEST_P(ZstdGpuDemoTests, PerStageTiming)
{
    RunPerformanceTest(GetParam(), 2, {"--d3d-gfx", "--seq-cnt"});
}

INSTANTIATE_TEST_SUITE_P(
    ContentTests,
    ZstdGpuDemoTests,
    ::testing::ValuesIn(GetTestFiles()),
    SanitizeTestName);

// Demo runner implementation
//
// Spawns zstdgpu_demo.exe as a child process via CreateProcess with anonymous
// pipes. A background thread drains stdout to avoid pipe-buffer deadlocks. If
// the child exceeds the configured timeout, it is terminated and CancelIoEx
// releases the reader thread so the wrapper does not itself hang.
//
// All GPU / D3D12 dependencies live in the demo process; the test binary
// itself has no GPU dependency.

namespace
{

// Builds a command line string with proper quoting for arguments containing spaces.
static std::string BuildCommandLine(const std::string& exe, const std::vector<std::string>& args)
{
    std::ostringstream cmd;
    cmd << "\"" << exe << "\"";
    for (const auto& arg : args)
    {
        cmd << " ";
        if (arg.find(' ') != std::string::npos)
            cmd << "\"" << arg << "\"";
        else
            cmd << arg;
    }
    return cmd.str();
}

DemoResult RunDemo(
    const std::string& demoPath,
    const std::vector<std::string>& args,
    int timeoutSeconds)
{
    DemoResult result;
    result.commandLine = BuildCommandLine(demoPath, args);

    // Create an anonymous pipe for capturing the child process's stdout/stderr.
    SECURITY_ATTRIBUTES sa{};
    sa.nLength = sizeof(sa);
    sa.bInheritHandle = TRUE;

    HANDLE hReadPipe = nullptr;
    HANDLE hWritePipe = nullptr;
    if (!CreatePipe(&hReadPipe, &hWritePipe, &sa, 0))
    {
        result.launchError = "Failed to create pipe for demo process.";
        return result;
    }

    // Prevent the read end from being inherited by the child process.
    SetHandleInformation(hReadPipe, HANDLE_FLAG_INHERIT, 0);

    // Redirect child's stdout and stderr to the write end of the pipe.
    STARTUPINFOA si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdOutput = hWritePipe;
    si.hStdError = hWritePipe;

    PROCESS_INFORMATION pi{};

    std::vector<char> cmdBuf(result.commandLine.begin(), result.commandLine.end());
    cmdBuf.push_back('\0');

    if (!CreateProcessA(
            nullptr,
            cmdBuf.data(),
            nullptr,
            nullptr,
            TRUE, // inherit handles
            0,
            nullptr,
            nullptr,
            &si,
            &pi))
    {
        CloseHandle(hReadPipe);
        CloseHandle(hWritePipe);
        result.launchError = "Failed to launch demo process. Error: " + std::to_string(GetLastError());
        return result;
    }

    // Close the write end in the parent so ReadFile on the read end returns
    // EOF when the child exits.
    CloseHandle(hWritePipe);

    // Read the child's output on a background thread to prevent pipe buffer
    // deadlocks (the pipe has a finite buffer; if it fills, the child blocks).
    std::string capturedOutput;
    std::thread readerThread([&capturedOutput, hReadPipe]() {
        std::array<char, 4096> buf;
        DWORD bytesRead = 0;
        while (ReadFile(hReadPipe, buf.data(), static_cast<DWORD>(buf.size()), &bytesRead, nullptr) && bytesRead > 0)
        {
            capturedOutput.append(buf.data(), bytesRead);
        }
    });

    // Wait for the child process, enforcing the timeout.
    DWORD waitMs = (timeoutSeconds > 0) ? static_cast<DWORD>(timeoutSeconds) * 1000 : INFINITE;
    DWORD waitResult = WaitForSingleObject(pi.hProcess, waitMs);

    if (waitResult == WAIT_TIMEOUT)
    {
        result.timedOut = true;
        TerminateProcess(pi.hProcess, 1);
        WaitForSingleObject(pi.hProcess, 5000);

        // Cancel any pending ReadFile on hReadPipe so the reader thread exits
        // and readerThread.join() below returns. Without this, TerminateProcess
        // on a wedged child can leave the child's inherited write-end of the
        // pipe orphaned in kernel state briefly, and the reader thread's
        // blocking ReadFile never returns — hanging the entire test runner
        // indefinitely. CancelIoEx targets outstanding I/O on the handle from
        // any thread. Safe to call even if no I/O is pending (returns FALSE
        // with ERROR_NOT_FOUND, harmless).
        CancelIoEx(hReadPipe, nullptr);
    }

    DWORD exitCode = 0;
    GetExitCodeProcess(pi.hProcess, &exitCode);
    result.exitCode = static_cast<int>(exitCode);

    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);

    // Wait for the reader thread to finish draining the pipe, then clean up.
    readerThread.join();
    CloseHandle(hReadPipe);

    result.stdOut = std::move(capturedOutput);
    return result;
}

// Builds argument list for correctness tests: decompress once (--run-cnt 1)
// with GPU and CPU validation enabled, plus scenario-specific flags.
std::vector<std::string> BuildCorrectnessArgs(
    const std::string& zstFile,
    const std::vector<std::string>& scenarioFlags)
{
    std::vector<std::string> args;
    args.push_back("--zst");
    args.push_back(zstFile);
    args.push_back("--run-cnt");
    args.push_back("1");
    for (const auto& flag : scenarioFlags)
    {
        args.push_back(flag);
    }
    return args;
}

// Builds argument list for performance tests: run N iterations at the specified
// profiling level, optionally writing per-run timing data to a CSV file. The
// scenario supplies any additional demo flags in `extraFlags` (e.g. --d3d-gfx
// and --seq-cnt for the PerStageTiming scenario).
std::vector<std::string> BuildPerformanceArgs(
    const std::string& zstFile,
    int profilingLevel,
    int runCount,
    const std::string& csvOutputPath,
    const std::vector<std::string>& extraFlags)
{
    std::vector<std::string> args;
    args.push_back("--zst");
    args.push_back(zstFile);
    args.push_back("--prf-lvl");
    args.push_back(std::to_string(profilingLevel));
    args.push_back("--run-cnt");
    args.push_back(std::to_string(runCount));
    if (!csvOutputPath.empty())
    {
        args.push_back("--out-csv");
        args.push_back(csvOutputPath);
    }
    for (const auto& flag : extraFlags)
    {
        args.push_back(flag);
    }
    return args;
}

} // namespace
