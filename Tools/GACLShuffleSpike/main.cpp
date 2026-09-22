// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

#include <cstdint>

#include "gacl.h"
#include "zstd.h"

#include <array>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <random>
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
    constexpr size_t kAlignedBlockCount = 4096;
    constexpr size_t kStragglerBlockCount = 4099;
    constexpr size_t kShuffleVersion = 1;
    constexpr int kZstdLevel = 12;

    enum class Format { BC1, BC3, BC4, BC5 };

    struct BC1Block { uint16_t color0; uint16_t color1; uint32_t indices; };
    struct BC3Block { uint8_t alpha0; uint8_t alpha1; uint8_t alphaIndices[6]; BC1Block color; };
    struct BC4Block { uint8_t red0; uint8_t red1; uint8_t indices[6]; };
    struct BC5Block { BC4Block red; BC4Block green; };
    static_assert(sizeof(BC1Block) == 8 && sizeof(BC3Block) == 16);
    static_assert(sizeof(BC4Block) == 8 && sizeof(BC5Block) == 16);

    struct TestCase
    {
        const char* name;
        Format format;
        size_t bytesPerBlock;
        HRESULT (*shuffle)(uint8_t*, const uint8_t*, size_t, size_t);
    };

    const std::array<TestCase, 4> kCases{{
        {"BC1", Format::BC1, sizeof(BC1Block), Shuffle_BC1},
        {"BC3", Format::BC3, sizeof(BC3Block), Shuffle_BC3},
        {"BC4", Format::BC4, sizeof(BC4Block), Shuffle_BC4},
        {"BC5", Format::BC5, sizeof(BC5Block), Shuffle_BC5},
    }};

    template <typename T> T Read(const uint8_t*& source)
    {
        T value{};
        std::memcpy(&value, source, sizeof(value));
        source += sizeof(value);
        return value;
    }

    void UnshuffleBC1(uint8_t* destination, const uint8_t* source, size_t count)
    {
        const uint8_t* color0 = source;
        const uint8_t* color1 = color0 + count * 2;
        const uint8_t* indices = color1 + count * 2;
        auto* blocks = reinterpret_cast<BC1Block*>(destination);
        for (size_t i = 0; i < count; ++i)
        {
            blocks[i].color0 = Read<uint16_t>(color0);
            blocks[i].color1 = Read<uint16_t>(color1);
            blocks[i].indices = Read<uint32_t>(indices);
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
        auto* blocks = reinterpret_cast<BC3Block*>(destination);
        for (size_t i = 0; i < count; ++i)
        {
            blocks[i].alpha0 = Read<uint8_t>(alpha0);
            blocks[i].alpha1 = Read<uint8_t>(alpha1);
            std::memcpy(blocks[i].alphaIndices, alphaIndices, 6); alphaIndices += 6;
            blocks[i].color.color0 = Read<uint16_t>(color0);
            blocks[i].color.color1 = Read<uint16_t>(color1);
            blocks[i].color.indices = Read<uint32_t>(colorIndices);
        }
    }

    void UnshuffleBC4(uint8_t* destination, const uint8_t* source, size_t count)
    {
        const uint8_t* red0 = source;
        const uint8_t* red1 = red0 + count;
        const uint8_t* indices = red1 + count;
        auto* blocks = reinterpret_cast<BC4Block*>(destination);
        for (size_t i = 0; i < count; ++i)
        {
            blocks[i].red0 = Read<uint8_t>(red0);
            blocks[i].red1 = Read<uint8_t>(red1);
            std::memcpy(blocks[i].indices, indices, 6); indices += 6;
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
        auto* blocks = reinterpret_cast<BC5Block*>(destination);
        for (size_t i = 0; i < count; ++i)
        {
            blocks[i].red.red0 = Read<uint8_t>(red0);
            blocks[i].red.red1 = Read<uint8_t>(red1);
            std::memcpy(blocks[i].red.indices, redIndices, 6); redIndices += 6;
            blocks[i].green.red0 = Read<uint8_t>(green0);
            blocks[i].green.red1 = Read<uint8_t>(green1);
            std::memcpy(blocks[i].green.indices, greenIndices, 6); greenIndices += 6;
        }
    }


    void Unshuffle(const TestCase& value, uint8_t* destination, const uint8_t* source, size_t count)
    {
        const size_t groupSize = value.format == Format::BC1 ? 2 : 4;
        const size_t shuffledCount = count - count % groupSize;
        switch (value.format)
        {
        case Format::BC1: UnshuffleBC1(destination, source, shuffledCount); break;
        case Format::BC3: UnshuffleBC3(destination, source, shuffledCount); break;
        case Format::BC4: UnshuffleBC4(destination, source, shuffledCount); break;
        case Format::BC5: UnshuffleBC5(destination, source, shuffledCount); break;
        }
        const size_t shuffledBytes = shuffledCount * value.bytesPerBlock;
        const size_t tailBytes = (count - shuffledCount) * value.bytesPerBlock;
        if (tailBytes != 0) std::memcpy(destination + shuffledBytes, source + shuffledBytes, tailBytes);
    }

    std::vector<uint8_t> MakeInput(const TestCase& value, size_t blockCount)
    {
        std::vector<uint8_t> input(blockCount * value.bytesPerBlock);
        std::mt19937 generator(0x4741434Cu + static_cast<uint32_t>(value.format));
        std::uniform_int_distribution<unsigned> distribution(0, 255);
        for (auto& byte : input) byte = static_cast<uint8_t>(distribution(generator));
        return input;
    }

    size_t CompressAndVerify(const std::vector<uint8_t>& input)
    {
        std::vector<uint8_t> compressed(ZSTD_compressBound(input.size()));
        const size_t compressedSize = ZSTD_compress(compressed.data(), compressed.size(), input.data(), input.size(), kZstdLevel);
        if (ZSTD_isError(compressedSize)) throw std::runtime_error(ZSTD_getErrorName(compressedSize));
        std::vector<uint8_t> restored(input.size());
        const size_t restoredSize = ZSTD_decompress(restored.data(), restored.size(), compressed.data(), compressedSize);
        if (ZSTD_isError(restoredSize) || restoredSize != input.size() || restored != input)
            throw std::runtime_error("Zstd round trip mismatch");
        return compressedSize;
    }

    void Run(const TestCase& value, const std::vector<uint8_t>& input, std::string_view label)
    {
        if (input.empty() || input.size() % value.bytesPerBlock != 0)
            throw std::runtime_error(std::string(value.name) + " input is not block aligned");
        std::vector<uint8_t> shuffled(input.size());
        if (FAILED(value.shuffle(shuffled.data(), input.data(), input.size(), kShuffleVersion)))
            throw std::runtime_error(std::string(value.name) + " shuffle failed");
        std::vector<uint8_t> restored(input.size());
        Unshuffle(value, restored.data(), shuffled.data(), input.size() / value.bytesPerBlock);
        if (restored != input) throw std::runtime_error(std::string(value.name) + " reverse mismatch");
        const size_t directSize = CompressAndVerify(input);
        const size_t shuffledSize = CompressAndVerify(shuffled);
        const double delta = 100.0 * (static_cast<double>(shuffledSize) - directSize) / directSize;
        std::cout << std::left << std::setw(4) << value.name << " label=" << label
                  << " input=" << input.size() << " direct_zstd=" << directSize
                  << " shuffle_zstd=" << shuffledSize << " delta=" << std::fixed
                  << std::setprecision(2) << delta << "% round_trip=PASS\n";
    }

    uint32_t ReadU32(const std::vector<uint8_t>& data, size_t offset)
    {
        if (offset + 4 > data.size()) throw std::runtime_error("Truncated DDS header");
        uint32_t value{};
        std::memcpy(&value, data.data() + offset, sizeof(value));
        return value;
    }

    std::vector<uint8_t> ReadFile(const std::filesystem::path& path)
    {
        std::ifstream stream(path, std::ios::binary | std::ios::ate);
        if (!stream) throw std::runtime_error("Could not open DDS: " + path.string());
        const auto end = stream.tellg();
        if (end < 0) throw std::runtime_error("Could not size DDS: " + path.string());
        std::vector<uint8_t> data(static_cast<size_t>(end));
        stream.seekg(0);
        if (!data.empty() && !stream.read(reinterpret_cast<char*>(data.data()), end))
            throw std::runtime_error("Could not read DDS: " + path.string());
        return data;
    }

    const TestCase& FindCase(std::string_view name)
    {
        for (const auto& value : kCases) if (name == value.name) return value;
        throw std::runtime_error("DDS format must be BC1, BC3, BC4, or BC5");
    }

    void RunDDS(const TestCase& value, const std::filesystem::path& path)
    {
        constexpr uint32_t ddsMagic = 0x20534444, dx10 = 0x30315844;
        constexpr uint32_t dxt1 = 0x31545844, dxt5 = 0x35545844;
        constexpr uint32_t bc4u = 0x55344342, bc5u = 0x55354342;
        const auto data = ReadFile(path);
        if (ReadU32(data, 0) != ddsMagic || ReadU32(data, 4) != 124)
            throw std::runtime_error("Invalid DDS header");
        size_t offset = 128;
        const uint32_t fourCC = ReadU32(data, 84);
        if (fourCC == dx10)
        {
            const uint32_t format = ReadU32(data, 128);
            offset = 148;
            const bool matches =
                (value.format == Format::BC1 && format >= 70 && format <= 72) ||
                (value.format == Format::BC3 && format >= 76 && format <= 78) ||
                (value.format == Format::BC4 && format >= 79 && format <= 81) ||
                (value.format == Format::BC5 && format >= 82 && format <= 84);
            if (!matches) throw std::runtime_error("DDS DXGI format mismatch");
        }
        else if (!((value.format == Format::BC1 && fourCC == dxt1) ||
                   (value.format == Format::BC3 && fourCC == dxt5) ||
                   (value.format == Format::BC4 && fourCC == bc4u) ||
                   (value.format == Format::BC5 && fourCC == bc5u)))
        {
            throw std::runtime_error("DDS FourCC mismatch");
        }
        const size_t width = ReadU32(data, 16);
        const size_t height = ReadU32(data, 12);
        const size_t blocksX = (width + 3) / 4;
        const size_t blocksY = (height + 3) / 4;
        if (blocksX != 0 && blocksY > SIZE_MAX / blocksX)
            throw std::runtime_error("DDS dimensions overflow block count");
        const size_t blockCount = blocksX * blocksY;
        if (blockCount > SIZE_MAX / value.bytesPerBlock)
            throw std::runtime_error("DDS first mip size overflows");
        const size_t size = blockCount * value.bytesPerBlock;
        if (offset > data.size() || size > data.size() - offset)
            throw std::runtime_error("DDS first mip exceeds file bounds");
        std::vector<uint8_t> mip(data.begin() + static_cast<std::ptrdiff_t>(offset),
                                 data.begin() + static_cast<std::ptrdiff_t>(offset + size));
        Run(value, mip, path.filename().string());
    }
}

int main(int argc, char** argv)
{
    try
    {
        for (const auto& value : kCases)
        {
            Run(value, MakeInput(value, kAlignedBlockCount), "synthetic-aligned");
            Run(value, MakeInput(value, kStragglerBlockCount), "synthetic-straggler");
        }
        if ((argc - 1) % 2 != 0)
            throw std::runtime_error("Usage: GACLShuffleSpike [BC1|BC3|BC4|BC5 <file.dds>]...");
        for (int i = 1; i < argc; i += 2) RunDDS(FindCase(argv[i]), argv[i + 1]);
        std::cout << "GACL shuffle-only spike: PASS\n";
        return 0;
    }
    catch (const std::exception& exception)
    {
        std::cerr << "GACL shuffle-only spike: FAIL: " << exception.what() << '\n';
        return 1;
    }
}
