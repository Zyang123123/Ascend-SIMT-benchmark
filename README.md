# Ascend-SIMT-benchmark

华为昇腾 DV100 (DaVinci C310) AI Core SIMT 带宽自动化测试框架。通过参数化内核模板 + Python 调度器，自动遍历多种变量组合，测量 GM/L2 内存的读写带宽，结果汇总到 CSV。

## 目录

- [项目结构](#项目结构)
- [环境准备](#环境准备)
- [快速开始](#快速开始)
- [变量因素说明](#变量因素说明)
- [使用方式](#使用方式)
  - [默认单组测试](#1-默认单组测试)
  - [使用预设扫描](#2-使用预设扫描)
  - [自定义因素扫描](#3-自定义因素扫描)
  - [预览测试计划](#4-预览测试计划dry-run)
  - [使用 msprof 采集性能数据](#5-使用-msprof-采集性能数据)
  - [指定设备和输出路径](#6-指定设备和输出路径)
- [预设扫描配置一览](#预设扫描配置一览)
- [自定义扫描参数](#自定义扫描参数)
- [输出格式](#输出格式)
- [框架工作原理](#框架工作原理)
- [单独编译和运行](#单独编译和运行)
- [常见问题](#常见问题)

## 项目结构

```
Ascend-SIMT-benchmark/
├── kernel_template.cce           # 内核模板（宏定义驱动，由脚本自动修改）
├── host_driver.cpp               # 主机端驱动（支持命令行参数）
├── compile_bench.sh              # 编译脚本（接受参数化编译）
├── run_benchmark.py              # 主调度器（Python，核心入口）
├── results/                      # 测试结果输出目录
│   └── benchmark_<timestamp>.csv
├── dv100_simt_bandwidth_test.cce       # [旧] 原始 fp16 内核
├── dv100_simt_bandwidth_test_host.cpp  # [旧] 原始主机端
├── compile.sh                           # [旧] 原始编译脚本
└── run_sweep.sh                         # [旧] 原始扫描脚本
```

## 环境准备

**硬件要求：** 华为昇腾 DV100 (DaVinci C310) NPU

**软件依赖：**

1. CANN 8.3.RC1（或兼容版本）
2. Python 3.7+
3. g++、ccec、ld.lld（CANN 工具链自带）

**环境变量设置：**

```bash
# source CANN 环境
source /path/to/Ascend/cann8.3/8.3.RC1/bin/setenv.bash
export ASCEND_HOME_PATH=/path/to/Ascend/cann8.3/8.3.RC1
```

## 快速开始

```bash
# 1. 设置 CANN 环境
source /path/to/Ascend/cann8.3/8.3.RC1/bin/setenv.bash
export ASCEND_HOME_PATH=/path/to/Ascend/cann8.3/8.3.RC1

# 2. 预览要测试的配置（不实际执行）
python3 run_benchmark.py --dry-run

# 3. 运行默认单组测试
python3 run_benchmark.py

# 4. 运行预设扫描（如 burst 扫描）
python3 run_benchmark.py --preset burst_sweep

# 5. 查看结果
cat results/benchmark_*.csv
```

## 变量因素说明

框架支持以下变量因素的自动扫描：

| 因素 | 参数名 | 可选值 | 说明 |
|------|--------|--------|------|
| 源地址位置 | `src_memory` | `0`=GM, `1`=L2 | GM=全局内存(HBM)；L2=先预热再测 |
| 数据类型 | `data_type` | `0`=fp16, `1`=float32, `2`=int64 | 对应 2/4/8 字节 |
| 访问模式 | `access_mode` | `0`=只读, `1`=只写, `2`=读+写 | 分别测量读带宽、写带宽、读写带宽 |
| stride 大小 | `stride` | 任意正整数（如 `32`, `64`, `128`） | 展开 loop 内相邻访问间隔（元素数） |
| burst 长度 | `lane_bits` | `3`~`9` | 对应 8~512 lanes/burst，控制连续突发长度 |
| block 内线程数 | `thread_num` | `64`, `128`, `256`, `512`, `1024` | 每个 block 的 SIMT 线程数 |
| block 数 | `block_num` | `1`~`128` | 启动的 block 数量（主机端参数） |
| 非对齐偏移 | `align_offset` | `0`=对齐, `1/2/4/8/16`=偏移 | 首地址偏移元素数，测试非对齐场景 |
| 数据量 | `data_size_mb` | `128`, `256`, `512` 等 | 总 buffer 大小（MB） |
| 重复次数 | `repeat` | `1`~`20` | 计时用重复次数，越多越稳定 |

**`lane_bits` 与 burst 的关系：**

```
lane_bits=3 → LANE_MASK=0b111       → 8  lanes/burst
lane_bits=5 → LANE_MASK=0b11111     → 32 lanes/burst
lane_bits=7 → LANE_MASK=0b1111111   → 128 lanes/burst
lane_bits=9 → LANE_MASK=0b111111111 → 512 lanes/burst
```

`UNROLL_LOOP` 会根据 `lane_bits` 自动计算：`UNROLL_LOOP = 32 / lane_bits`。

## 使用方式

### 1. 默认单组测试

不传任何参数时，使用 `run_benchmark.py` 中的 `DEFAULT_SWEEP` 配置（默认全部因素各取一个值，即只测一组）：

```bash
python3 run_benchmark.py
```

默认配置等价于：GM 读、fp16、stride=32、512 lanes、1024 线程/block、64 blocks、512MB、对齐。

### 2. 使用预设扫描

通过 `--preset` 选择内置的扫描配置：

```bash
# 只扫描不同数据类型
python3 run_benchmark.py --preset dtype_sweep

# 只扫描 burst 长度 (3~9 bits)
python3 run_benchmark.py --preset burst_sweep

# 只扫描 block 数量
python3 run_benchmark.py --preset block_sweep

# 只扫描访问模式（读/写/读写）
python3 run_benchmark.py --preset access_sweep

# 只扫描对齐偏移
python3 run_benchmark.py --preset align_sweep

# 只扫描 stride
python3 run_benchmark.py --preset stride_sweep

# 只扫描线程数
python3 run_benchmark.py --preset thread_sweep

# 全量扫描（所有因素组合，耗时长）
python3 run_benchmark.py --preset full
```

### 3. 自定义因素扫描

通过 `--factors` 指定要扫描的因素（其余因素保持默认单值）：

```bash
# 同时扫描数据类型和 block 数量（交叉组合）
python3 run_benchmark.py --factors data_type block_num

# 扫描 stride 和 lane_bits
python3 run_benchmark.py --factors stride lane_bits
```

如需自定义具体扫描值，直接编辑 `run_benchmark.py` 中的 `DEFAULT_SWEEP` 字典：

```python
DEFAULT_SWEEP = {
    "src_memory":   [0, 1],               # 扫描 GM 和 L2
    "data_type":    [0, 1, 2],            # 扫描 fp16, float32, int64
    "access_mode":  [0],                  # 只测只读
    "stride":       [32, 64, 128],        # 扫描多种 stride
    "lane_bits":    [7, 9],               # 只测 128 和 512 lanes
    "thread_num":   [1024],               # 固定 1024 线程
    "block_num":    [32, 64, 128],        # 扫描多种 block 数
    "align_offset": [0],                  # 对齐
    "data_size_mb": [512],                # 固定 512MB
    "repeat":       [5],                  # 重复 5 次
}
```

每个因素的列表中写多个值即会自动做笛卡尔积全组合。

### 4. 预览测试计划（dry-run）

不实际编译运行，只打印将要测试的所有配置组合：

```bash
python3 run_benchmark.py --preset burst_sweep --dry-run
```

输出示例：
```
================================================================
DRY RUN - 7 configurations, 7 compilations
================================================================

  [   1]  GM    fp16  read_only stride= 32 lanes=  8 threads=1024 blocks= 64 align=0 size=512MB
  [   2]  GM    fp16  read_only stride= 32 lanes= 16 threads=1024 blocks= 64 align=0 size=512MB
  ...
```

### 5. 使用 msprof 采集性能数据

加上 `--msprof` 参数，每次运行会通过 msprof 采集 task-based profiling 数据：

```bash
python3 run_benchmark.py --preset burst_sweep --msprof
```

msprof 数据保存在 `results/msprof/PROF_*` 目录下。

### 6. 指定设备和输出路径

```bash
# 使用设备 1
python3 run_benchmark.py --device 1

# 指定输出 CSV 路径
python3 run_benchmark.py -o my_results.csv

# 组合使用
python3 run_benchmark.py --preset dtype_sweep --device 0 -o dtype_results.csv
```

## 预设扫描配置一览

| 预设名 | 扫描因素 | 配置数 | 说明 |
|--------|----------|--------|------|
| `dtype_sweep` | data_type: [0,1,2] | 3 | fp16 / float32 / int64 读写带宽对比 |
| `burst_sweep` | lane_bits: [3..9] | 7 | 不同 burst 长度对带宽的影响 |
| `block_sweep` | block_num: [1..128] | 8 | 不同并行度对带宽的影响 |
| `access_sweep` | access_mode: [0,1,2] | 3 | 只读 / 只写 / 读写带宽对比 |
| `align_sweep` | align_offset: [0..16] | 6 | 非对齐访问对带宽的影响 |
| `stride_sweep` | stride: [1..512] | 10 | 不同访问 stride 对带宽的影响 |
| `thread_sweep` | thread_num: [64..1024] | 5 | 不同线程数对带宽的影响 |
| `full` | 多因素全组合 | ~500 | 全量扫描（耗时长） |

## 自定义扫描参数

有两种方式自定义：

### 方式一：修改 DEFAULT_SWEEP

直接编辑 `run_benchmark.py` 中第 53 行的 `DEFAULT_SWEEP` 字典：

```python
DEFAULT_SWEEP = {
    "data_type":  [0, 1],      # 只测 fp16 和 float32
    "block_num":  [64, 128],   # 只测 64 和 128 blocks
    # ... 其余保持单值
}
```

然后运行：

```bash
python3 run_benchmark.py
```

### 方式二：添加新 PRESET

在 `run_benchmark.py` 的 `PRESETS` 字典（第 357 行）中添加自定义预设：

```python
PRESETS = {
    # ... 已有预设 ...

    "my_custom": {
        "src_memory":   [0],
        "data_type":    [0, 1],
        "access_mode":  [0, 2],
        "stride":       [32],
        "lane_bits":    [9],
        "thread_num":   [1024],
        "block_num":    [64],
        "align_offset": [0, 4],
        "data_size_mb": [256, 512],
        "repeat":       [5],
    },
}
```

然后运行：

```bash
python3 run_benchmark.py --preset my_custom
```

## 输出格式

每次运行生成一个 CSV 文件，默认保存在 `results/` 目录下。

**CSV 列说明：**

| 列名 | 类型 | 说明 |
|------|------|------|
| `timestamp` | string | 测试时间 ISO 格式 |
| `src_memory` | int | 0=GM, 1=L2 |
| `src_memory_name` | string | GM / L2 |
| `data_type` | int | 0=fp16, 1=float32, 2=int64 |
| `data_type_name` | string | fp16 / float32 / int64 |
| `dtype_size` | int | 数据类型字节数 (2/4/8) |
| `access_mode` | int | 0=只读, 1=只写, 2=读写 |
| `access_mode_name` | string | read_only / write_only / read_write |
| `stride` | int | 展开访问 stride（元素数） |
| `lane_bits` | int | LANE_MASK 位数 |
| `lane_mask` | string | LANE_MASK 二进制字面量 |
| `lanes` | int | 每个 burst 的 lane 数 |
| `thread_num` | int | 每 block 线程数 |
| `block_num` | int | block 数量 |
| `align_offset` | int | 对齐偏移（元素数） |
| `data_size_mb` | int | buffer 大小 MB |
| `repeat` | int | 计时重复次数 |
| `bytes_moved` | float | 实际搬移字节数 |
| `time_us` | int | 平均耗时（微秒） |
| `bandwidth_gbs` | float | 带宽（GB/s） |

**运行结束时的终端摘要：**

```
================================================================
Benchmark sweep complete!
  Total time:  45.2s
  Results:     7 rows
  Successful:  7
  Failed:      0
  Output CSV:  results/benchmark_20260506_170500.csv

  Bandwidth stats:
    Min:  320.45 GB/s
    Max:  856.12 GB/s
    Mean: 612.38 GB/s
================================================================
```

## 框架工作原理

```
run_benchmark.py
    │
    ├─ 1. 读取 DEFAULT_SWEEP 或 PRESET 配置
    │     生成所有参数的笛卡尔积组合
    │
    ├─ 2. 按编译参数去重分组
    │     （data_type/src_memory/access_mode/stride/lane_bits/thread_num/align_offset
    │      影响编译；block_num/data_size_mb/repeat 不影响编译）
    │
    ├─ 3. 对每组编译配置：
    │     ├─ patch_kernel(): 用正则替换 kernel_template.cce 中的宏定义
    │     │   → 生成 bench_kernel_src.cce（临时文件）
    │     ├─ compile_bench.sh: 编译主机端 + 设备端
    │     │   → 生成 host + bench_kernel.o
    │     └─ 对组内每个运行配置：
    │         └─ 运行 ./host [size] [blocks] [repeat] [device]
    │            → 解析 BANDWIDTH_RESULT 行
    │
    └─ 4. 汇总所有结果 → 写入 CSV
```

**编译时参数 vs 运行时参数：**

| 类型 | 参数 | 是否需要重新编译 |
|------|------|-----------------|
| 编译时 | data_type, src_memory, access_mode, stride, lane_bits, thread_num, align_offset | 是 |
| 运行时 | block_num, data_size_mb, repeat, device_id | 否 |

框架自动按编译参数去重分组，同一编译配置只编译一次，然后遍历所有运行时参数。

## 单独编译和运行

如果不想用 Python 框架，也可以手动编译和运行：

```bash
# 编译（需要先设置 ASCEND_HOME_PATH）
bash compile_bench.sh kernel_template.cce host_driver.cpp 0
#                                                                ↑ dtype_enum (0=fp16)

# 运行
./host 512 64 5 0
#      ↑MB  ↑blocks ↑repeat ↑device

# 输出
# BANDWIDTH_RESULT,536870912,1234,432.100,0,0
# Time cost : 1.234 ms
# Bandwidth : 432.10 GB/s
```

## 常见问题

### Q: 如何只测一种数据类型？

修改 `DEFAULT_SWEEP` 中 `"data_type": [0]`（只测 fp16），或在已有预设基础上用 `--factors`：

```bash
python3 run_benchmark.py --preset block_sweep  # block_sweep 本身已固定 fp16
```

### Q: 如何添加新的数据类型？

1. 在 `kernel_template.cce` 中添加新的 `DATA_TYPE_ENUM` 分支（如 `#elif DATA_TYPE_ENUM == 3`）
2. 在 `host_driver.cpp` 中添加对应的 `HOST_DTYPE_ENUM` 分支
3. 在 `run_benchmark.py` 的 `DTYPE_INFO` 字典中添加元数据
4. 在 sweep 配置中加入新值

### Q: 编译失败怎么办？

1. 确认 `ASCEND_HOME_PATH` 已正确设置
2. 确认已 source CANN 的 `setenv.bash`
3. 检查 `ccec`、`ld.lld` 是否在 PATH 中
4. 用 `--dry-run` 先预览配置，确认参数合法

### Q: L2 带宽测试的原理？

设置 `src_memory=1` 时，内核会在正式测量前先遍历一次全部数据（warmup），将数据加载到 L2 cache，然后再测量访问性能。这测量的是 L2 cache 命中时的带宽。

### Q: 如何解读非对齐测试结果？

`align_offset` 控制首地址偏移的元素数。偏移后地址可能不再对齐到 512B/32B 边界，带宽可能下降。通过对比 `align_offset=0` 和其他值的带宽，可以评估对齐对性能的影响。
