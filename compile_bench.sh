#!/bin/bash
set -ex
# ============================================================
# compile_bench.sh - Flexible compile script for bandwidth benchmark
# ============================================================
# Usage: bash compile_bench.sh [kernel_source] [host_source] [dtype_enum]
#   kernel_source : kernel .cce file (default: bench_kernel_src.cce)
#   host_source   : host .cpp file (default: host_driver.cpp)
#   dtype_enum    : data type enum for host (0=fp16, 1=float32, 2=int64)
#
# Output:
#   host              - compiled host binary
#   bench_kernel.o    - compiled device binary (loaded by host)
# ============================================================

KERNEL_SRC="${1:-bench_kernel_src.cce}"
HOST_SRC="${2:-host_driver.cpp}"
DTYPE_ENUM="${3:-0}"

# Source CANN environment if not already set
# source /home/w00951285/Ascend/cann8.3/8.3.RC1/bin/setenv.bash
# export ASCEND_HOME_PATH=/home/w00951285/Ascend/cann8.3/8.3.RC1

if [ -z "${ASCEND_HOME_PATH}" ]; then
    echo "ERROR: ASCEND_HOME_PATH is not set. Please source CANN setenv.bash first."
    exit 1
fi

echo "=== Compiling host ==="
echo "  Host source: ${HOST_SRC}"
echo "  DTYPE_ENUM:  ${DTYPE_ENUM}"

g++ \
  -DHOST_DTYPE_ENUM=${DTYPE_ENUM} \
  -I${ASCEND_HOME_PATH}/include \
  -I${ASCEND_HOME_PATH}/include/experiment/msprof \
  -o host \
  ${HOST_SRC} \
  -L${ASCEND_HOME_PATH}/lib64 -lruntime -lascendcl

echo "=== Compiling device kernel ==="
echo "  Kernel source: ${KERNEL_SRC}"

ccec \
  --cce-aicore-arch=dav-c310-vec --cce-aicore-only \
  -mllvm --cce-vf-auto-sync=global \
  -mllvm -cce-aicore-dcci-insert-for-scalar=false \
  --cce-aicore-input-parameter-size=1536 \
  -O2 --std=c++17 \
  -I${ASCEND_HOME_PATH}/include \
  -o bench_kernel.o \
  ${KERNEL_SRC}

echo "=== Linking device binary ==="
ld.lld \
  -m aicorelinux -Ttext=0 \
  -static -o bench_kernel.o \
  bench_kernel.o

echo "=== Build complete ==="
echo "  host binary:    ./host"
echo "  device binary:  ./bench_kernel.o"
