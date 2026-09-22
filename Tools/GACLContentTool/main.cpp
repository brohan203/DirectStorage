// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

#include "gacl.h"
#include "shuffle.h"
#include "zstd.h"

#include <array>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

HRESULT Shuffle_BC1(uint8_t*, const uint8_t*, size_t, size_t);
HRESULT Shuffle_BC3(uint8_t*, const uint8_t*, size_t, size_t);
HRESULT Shuffle_BC4(uint8_t*, const uint8_t*, size_t, size_t);
HRESULT Shuffle_BC5(uint8_t*, const uint8_t*, size_t, size_t);

namespace
{
    constexpr std::string_view kVersion = "1.1.0";
    constexpr size_t kShuffleVersion = 1;

    struct BlockCase
    {
        const char* name;
        size_t bytesPerBlock;
        size_t groupSize;
        HRESULT (*shuffle)(uint8_t*, const uint8_t*, size_t, size_t);
        uint32_t transformId;
    };

    const std::array<BlockCase, 5> kCases{{
        {"BC1", 8, 2, Shuffle_BC1, GACL_SHUFFLE_TRANSFORM_ZSTD_BC1_224},
        {"BC3", 16, 4, Shuffle_BC3, GACL_SHUFFLE_TRANSFORM_ZSTD_BC3_116224},
        {"BC4", 8, 4, Shuffle_BC4, GACL_SHUFFLE_TRANSFORM_ZSTD_BC4_116},
        {"BC5", 16, 4, Shuffle_BC5, GACL_SHUFFLE_TRANSFORM_ZSTD_BC5_116116},
        {"BC7", 16, 1, nullptr, GACL_SHUFFLE_TRANSFORM_ZSTD_ONLY},
    }};
    struct LocalBC1Block { uint16_t color0; uint16_t color1; uint32_t indices; };
    struct LocalBC3Block { uint8_t alpha0; uint8_t alpha1; uint8_t alphaIndices[6]; LocalBC1Block color; };
    struct LocalBC4Block { uint8_t endpoint0; uint8_t endpoint1; uint8_t indices[6]; };
    struct LocalBC5Block { LocalBC4Block red; LocalBC4Block green; };
    static_assert(sizeof(LocalBC1Block) == 8 && sizeof(LocalBC3Block) == 16);
    static_assert(sizeof(LocalBC4Block) == 8 && sizeof(LocalBC5Block) == 16);

    template <typename T> T Read(const uint8_t*& source)
    {
        T value{};
        std::memcpy(&value, source, sizeof(value));
        source += sizeof(value);
        return value;
    }

    std::vector<uint8_t> ReadFile(const std::filesystem::path& path)
    {
        std::ifstream stream(path, std::ios::binary | std::ios::ate);
        if (!stream) throw std::runtime_error("could not open input: " + path.string());
        const auto end = stream.tellg();
        if (end < 0) throw std::runtime_error("could not size input: " + path.string());
        std::vector<uint8_t> data(static_cast<size_t>(end));
        stream.seekg(0);
        if (!data.empty() && !stream.read(reinterpret_cast<char*>(data.data()), end))
            throw std::runtime_error("could not read input: " + path.string());
        return data;
    }

    void WriteFile(const std::filesystem::path& path, const std::vector<uint8_t>& data)
    {
        std::ofstream stream(path, std::ios::binary | std::ios::trunc);
        if (!stream) throw std::runtime_error("could not open output: " + path.string());
        if (!data.empty()) stream.write(reinterpret_cast<const char*>(data.data()), static_cast<std::streamsize>(data.size()));
        if (!stream) throw std::runtime_error("could not write output: " + path.string());
    }

    const BlockCase& FindCase(std::string_view name)
    {
        for (const auto& value : kCases) if (name == value.name) return value;
        throw std::runtime_error("format must be BC1, BC3, BC4, BC5, or BC7");
    }

    int ParseInteger(std::string_view text, const char* label, int minimum, int maximum)
    {
        size_t used = 0;
        const long parsed = std::stol(std::string(text), &used, 10);
        if (used != text.size() || parsed < minimum || parsed > maximum)
            throw std::runtime_error(std::string(label) + " is out of range");
        return static_cast<int>(parsed);
    }

    void UnshuffleBC1(uint8_t* destination, const uint8_t* source, size_t count)
    {
        const uint8_t* color0 = source;
        const uint8_t* color1 = color0 + count * 2;
        const uint8_t* indices = color1 + count * 2;
        auto* blocks = reinterpret_cast<LocalBC1Block*>(destination);
        for (size_t index = 0; index < count; ++index)
        {
            blocks[index].color0 = Read<uint16_t>(color0);
            blocks[index].color1 = Read<uint16_t>(color1);
            blocks[index].indices = Read<uint32_t>(indices);
        }
    }

    void UnshuffleBC3(uint8_t* destination, const uint8_t* source, size_t count)
    {
        const uint8_t* alpha0 = source;
        const uint8_t* alpha1 = alpha0 + count;
        const uint8_t* alphaIndices = alpha1 + count;
        const uint8_t* color0 = alphaIndices + count * 6;
        const uint8_t* color1 = color0 + count * 2;
        const uint8_t* colorIndices = color1 + count * 2;
        auto* blocks = reinterpret_cast<LocalBC3Block*>(destination);
        for (size_t index = 0; index < count; ++index)
        {
            blocks[index].alpha0 = Read<uint8_t>(alpha0);
            blocks[index].alpha1 = Read<uint8_t>(alpha1);
            std::memcpy(blocks[index].alphaIndices, alphaIndices, 6); alphaIndices += 6;
            blocks[index].color.color0 = Read<uint16_t>(color0);
            blocks[index].color.color1 = Read<uint16_t>(color1);
            blocks[index].color.indices = Read<uint32_t>(colorIndices);
        }
    }

    void UnshuffleBC4(uint8_t* destination, const uint8_t* source, size_t count)
    {
        const uint8_t* endpoint0 = source;
        const uint8_t* endpoint1 = endpoint0 + count;
        const uint8_t* indices = endpoint1 + count;
        auto* blocks = reinterpret_cast<LocalBC4Block*>(destination);
        for (size_t index = 0; index < count; ++index)
        {
            blocks[index].endpoint0 = Read<uint8_t>(endpoint0);
            blocks[index].endpoint1 = Read<uint8_t>(endpoint1);
            std::memcpy(blocks[index].indices, indices, 6); indices += 6;
        }
    }

    void UnshuffleBC5(uint8_t* destination, const uint8_t* source, size_t count)
    {
        const uint8_t* red0 = source;
        const uint8_t* red1 = red0 + count;
        const uint8_t* redIndices = red1 + count;
        const uint8_t* green0 = redIndices + count * 6;
        const uint8_t* green1 = green0 + count;
        const uint8_t* greenIndices = green1 + count;
        auto* blocks = reinterpret_cast<LocalBC5Block*>(destination);
        for (size_t index = 0; index < count; ++index)
        {
            blocks[index].red.endpoint0 = Read<uint8_t>(red0);
            blocks[index].red.endpoint1 = Read<uint8_t>(red1);
            std::memcpy(blocks[index].red.indices, redIndices, 6); redIndices += 6;
            blocks[index].green.endpoint0 = Read<uint8_t>(green0);
            blocks[index].green.endpoint1 = Read<uint8_t>(green1);
            std::memcpy(blocks[index].green.indices, greenIndices, 6); greenIndices += 6;
        }
    }

    void Unshuffle(const BlockCase& value, uint8_t* destination, const uint8_t* source, size_t blockCount)
    {
        if (value.transformId == 7)
        {
            std::memcpy(destination, source, blockCount * value.bytesPerBlock);
            return;
        }
        const size_t shuffledCount = blockCount - blockCount % value.groupSize;
        if (value.transformId == 1) UnshuffleBC1(destination, source, shuffledCount);
        else if (value.transformId == 2) UnshuffleBC3(destination, source, shuffledCount);
        else if (value.transformId == 3) UnshuffleBC4(destination, source, shuffledCount);
        else UnshuffleBC5(destination, source, shuffledCount);
        const size_t shuffledBytes = shuffledCount * value.bytesPerBlock;
        const size_t tailBytes = (blockCount - shuffledCount) * value.bytesPerBlock;
        if (tailBytes != 0) std::memcpy(destination + shuffledBytes, source + shuffledBytes, tailBytes);
    }

    std::vector<uint8_t> Compress(const BlockCase& value, const std::vector<uint8_t>& input, int level, int targetBlockSize)
    {
        if (input.empty() || input.size() % value.bytesPerBlock != 0)
            throw std::runtime_error("input must contain a non-empty, block-aligned BC payload");
        std::vector<uint8_t> shuffled(input.size());
        if (value.shuffle != nullptr)
        {
            if (FAILED(value.shuffle(shuffled.data(), input.data(), input.size(), kShuffleVersion)))
                throw std::runtime_error("GACL stable shuffle failed");
        }
        else
        {
            shuffled = input;
        }

        ZSTD_CCtx* context = ZSTD_createCCtx();
        if (!context) throw std::runtime_error("could not create Zstd context");
        const ZSTD_compressionParameters params = ZSTD_getCParams(level, input.size(), 0);
        size_t status = ZSTD_CCtx_setCParams(context, params);
        if (!ZSTD_isError(status)) status = ZSTD_CCtx_setParameter(context, ZSTD_c_targetCBlockSize, targetBlockSize);
        std::vector<uint8_t> compressed(ZSTD_compressBound(shuffled.size()));
        if (!ZSTD_isError(status)) status = ZSTD_compress2(context, compressed.data(), compressed.size(), shuffled.data(), shuffled.size());
        ZSTD_freeCCtx(context);
        if (ZSTD_isError(status)) throw std::runtime_error(ZSTD_getErrorName(status));
        compressed.resize(status);

        std::vector<uint8_t> restoredShuffle(input.size());
        const size_t restoredSize = ZSTD_decompress(restoredShuffle.data(), restoredShuffle.size(), compressed.data(), compressed.size());
        if (ZSTD_isError(restoredSize) || restoredSize != input.size())
            throw std::runtime_error("Zstd verification failed");
        std::vector<uint8_t> restored(input.size());
        Unshuffle(value, restored.data(), restoredShuffle.data(), input.size() / value.bytesPerBlock);
        if (restored != input) throw std::runtime_error("GACL reverse verification failed");
        return compressed;
    }

    void Verify(const BlockCase& value, const std::vector<uint8_t>& original, const std::vector<uint8_t>& compressed)
    {
        if (original.empty() || original.size() % value.bytesPerBlock != 0)
            throw std::runtime_error("original payload must be non-empty and block-aligned");
        std::vector<uint8_t> shuffled(original.size());
        const size_t size = ZSTD_decompress(shuffled.data(), shuffled.size(), compressed.data(), compressed.size());
        if (ZSTD_isError(size) || size != original.size()) throw std::runtime_error("Zstd verification failed");
        std::vector<uint8_t> restored(original.size());
        Unshuffle(value, restored.data(), shuffled.data(), original.size() / value.bytesPerBlock);
        if (restored != original) throw std::runtime_error("GACL reverse verification failed");
    }

    std::string Value(int argc, char** argv, const char* name)
    {
        for (int index = 1; index + 1 < argc; ++index)
            if (std::string_view(argv[index]) == name) return argv[index + 1];
        throw std::runtime_error(std::string("missing ") + name);
    }
}

int main(int argc, char** argv)
{
    try
    {
        if (argc == 2 && std::string_view(argv[1]) == "--version")
        {
            std::cout << "GACLContentTool " << kVersion << '\n';
            return 0;
        }
        const bool verify = argc > 1 && std::string_view(argv[1]) == "--verify";
        const auto& format = FindCase(Value(argc, argv, "--format"));
        if (verify)
        {
            const auto original = ReadFile(Value(argc, argv, "--original"));
            const auto compressed = ReadFile(Value(argc, argv, "--input"));
            Verify(format, original, compressed);
            std::cout << "GACL verification passed; transform_id=" << format.transformId << '\n';
            return 0;
        }
        const int level = ParseInteger(Value(argc, argv, "--zstd-level"), "zstd level", 1, 22);
        const int targetBlockSize = ParseInteger(Value(argc, argv, "--target-block-size"), "target block size", 1, 1 << 20);
        const auto input = ReadFile(Value(argc, argv, "--input"));
        const auto output = std::filesystem::path(Value(argc, argv, "--output"));
        const auto compressed = Compress(format, input, level, targetBlockSize);
        WriteFile(output, compressed);
        std::cout << "transform_id=" << format.transformId << " uncompressed_size=" << input.size()
                  << " compressed_size=" << compressed.size() << '\n';
        return 0;
    }
    catch (const std::exception& exception)
    {
        std::cerr << "error: " << exception.what() << '\n';
        return 1;
    }
}
