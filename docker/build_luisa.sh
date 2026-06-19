#!/bin/bash
set -e

# Usage:
# ./build_luisa.sh <PYTHON_VERSION> [BUILD_JOBS]
# Example:
# ./build_luisa.sh 3.12 2

if [ -z "$1" ]; then
    echo "Usage: $0 <PYTHON_VERSION> [BUILD_JOBS]"
    exit 1
fi

PYTHON_VERSION=$1
BUILD_JOBS=${2:-2}

echo "Using Python version: ${PYTHON_VERSION}"
echo "Using build jobs: ${BUILD_JOBS}"

cd /workspace/Genesis/genesis/ext/LuisaRender

git submodule update --init --recursive

mkdir -p build

cmake -S . -B build \
    -D CMAKE_BUILD_TYPE=Release \
    -D CMAKE_POLICY_VERSION_MINIMUM=3.5 \
    -D PYTHON_VERSIONS=${PYTHON_VERSION} \
    -D LUISA_COMPUTE_DOWNLOAD_NVCOMP=ON \
    -D LUISA_COMPUTE_DOWNLOAD_OIDN=ON \
    -D LUISA_COMPUTE_ENABLE_GUI=OFF \
    -D LUISA_COMPUTE_ENABLE_CUDA=ON \
    -D CMAKE_CUDA_ARCHITECTURES=89 \
    -D pybind11_DIR=$(python3 -c "import pybind11; print(pybind11.get_cmake_dir())")

cmake --build build --parallel ${BUILD_JOBS}