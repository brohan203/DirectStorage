cmake_minimum_required(VERSION 3.20)

project(GACLShuffleSpike LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT MSVC)
    message(FATAL_ERROR "GACLShuffleSpike currently requires MSVC and the Windows SDK")
endif()

set(GACL_ROOT "" CACHE PATH "Path to a pinned Game Asset Conditioning Library checkout")
if(NOT GACL_ROOT)
    message(FATAL_ERROR "Set GACL_ROOT to the pinned GACL checkout")
endif()

get_filename_component(GACL_ROOT "${GACL_ROOT}" ABSOLUTE)
if(NOT EXISTS "${GACL_ROOT}/Api/includes/shuffle.h")
    message(FATAL_ERROR "GACL_ROOT does not contain Api/includes/shuffle.h: ${GACL_ROOT}")
endif()
if(NOT EXISTS "${GACL_ROOT}/ThirdParty/zstd/lib/zstd.h")
    message(FATAL_ERROR "Initialize the pinned GACL ThirdParty/zstd submodule")
endif()

set(ZSTD_BUILD_PROGRAMS OFF CACHE BOOL "" FORCE)
set(ZSTD_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(ZSTD_BUILD_SHARED OFF CACHE BOOL "" FORCE)
set(ZSTD_BUILD_STATIC ON CACHE BOOL "" FORCE)
set(ZSTD_MULTITHREAD_SUPPORT OFF CACHE BOOL "" FORCE)
add_subdirectory("${GACL_ROOT}/ThirdParty/zstd/build/cmake" "${CMAKE_BINARY_DIR}/zstd" EXCLUDE_FROM_ALL)

add_library(gacl_shuffle_reduced STATIC
    "${GACL_ROOT}/Lib/shuffle/bc1.cpp"
    "${GACL_ROOT}/Lib/shuffle/bc3.cpp"
    "${GACL_ROOT}/Lib/shuffle/bc4.cpp"
    "${GACL_ROOT}/Lib/shuffle/bc5.cpp"
)
target_include_directories(gacl_shuffle_reduced PUBLIC
    "${GACL_ROOT}/Api/includes"
    "${GACL_ROOT}/Lib"
)
target_compile_definitions(gacl_shuffle_reduced PUBLIC
    GACL_INCLUDE_BLER=0
    GACL_INCLUDE_CLER=0
    UNICODE
    _UNICODE
)

add_executable(GACLShuffleSpike main.cpp)
target_include_directories(GACLShuffleSpike PRIVATE
    "${GACL_ROOT}/Api/includes"
    "${GACL_ROOT}/Lib"
    "${GACL_ROOT}/Lib/shuffle"
    "${GACL_ROOT}/ThirdParty/zstd/lib"
)
target_compile_definitions(GACLShuffleSpike PRIVATE
    GACL_INCLUDE_BLER=0
    GACL_INCLUDE_CLER=0
)
target_link_libraries(GACLShuffleSpike PRIVATE gacl_shuffle_reduced libzstd_static)
target_link_libraries(GACLShuffleSpike PRIVATE gacl_shuffle_reduced libzstd_static)

target_compile_options(gacl_shuffle_reduced PRIVATE /W4 /WX)
target_compile_options(GACLShuffleSpike PRIVATE /W4 /WX)
