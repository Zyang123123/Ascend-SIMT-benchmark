#!/bin/bash
set -ex
# 记得source CANN 8.3.RC1 的set_env.sh

# source /home/w00951285/Ascend/cann8.3/8.3.RC1/bin/setenv.bash
# export ASCEND_HOME_PATH=/home/w00951285/Ascend/cann8.3/8.3.RC1

g++ \
  -I${ASCEND_HOME_PATH}/include \
  -I${ASCEND_HOME_PATH}/include/experiment/msprof \
  -o host \
  dv100_simt_bandwidth_test_host.cpp \
  -L${ASCEND_HOME_PATH}/lib64 -lruntime -lascendcl


ccec \
  --cce-aicore-arch=dav-c310-vec --cce-aicore-only \
  -mllvm --cce-vf-auto-sync=global \
  -mllvm -cce-aicore-dcci-insert-for-scalar=false \
  --cce-aicore-input-parameter-size=1536 \
  -O2 --std=c++17 \
  -I${ASCEND_HOME_PATH}/include \
  -o dv100_simt_bandwidth_test.o \
  dv100_simt_bandwidth_test.cce

ld.lld \
  -m aicorelinux -Ttext=0 \
  -static -o dv100_simt_bandwidth_test.o \
  dv100_simt_bandwidth_test.o