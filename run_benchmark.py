#!/usr/bin/env python3
"""
Ascend DV100 SIMT Bandwidth Benchmark - Automated Test Framework

Usage:
    # Run full sweep with default configuration
    python3 run_benchmark.py

    # Run only specific factors
    python3 run_benchmark.py --factors data_type block_num

    # Run with custom config
    python3 run_benchmark.py --config my_config.py

    # Dry run (show what would be tested, don't execute)
    python3 run_benchmark.py --dry-run

    # Use msprof for profiling
    python3 run_benchmark.py --msprof

Sweep factors (configurable):
    src_memory   : source memory location [0=GM, 1=L2]
    data_type    : data type enum [0=fp16, 1=float32, 2=int64]
    access_mode  : access pattern [0=read, 1=write, 2=readwrite]
    stride       : stride between unrolled accesses [32, 64, 128, ...]
    lane_bits    : bits for LANE_MASK (3..9 => 8..512 lanes)
    thread_num   : threads per block [256, 512, 1024]
    block_num    : number of blocks [16, 32, 64, 128]
    align_offset : alignment offset in elements [0, 1, 2, 4]
    data_size_mb : total buffer size in MB [128, 256, 512]
"""

import os
import sys
import copy
import shutil
import subprocess
import argparse
import csv
import re
import time
from itertools import product
from datetime import datetime
from pathlib import Path

# ============================================================
# Sweep Configuration
# ============================================================
# Each factor maps to a list of values to sweep.
# Comment out or remove values you don't want to test.
# Set a factor to a single-element list to fix it.

DEFAULT_SWEEP = {
    "src_memory":   [0],                   # 0=GM, 1=L2(cache warmup)
    "data_type":    [2],                   # 0=fp16, 1=float32, 2=int64
    "access_mode":  [0],                   # 0=read, 1=write, 2=readwrite
    "stride_factor": [1],                  # >0: STRIDE = factor * 2^lane_bits; 0: use absolute stride below
    "stride":       [1200],                   # absolute stride in elements (only used when stride_factor=0)
    "lane_bits":    [9],                   # LANE_MASK bits (3..9)
    "unroll_loop":  [4],                   # unroll factor for inner loop
    "thread_num":   [256,512,1024],                # threads per block
    "block_num":    [128],                  # number of blocks (host-side)
    "align_offset": [0],                   # alignment offset (0=aligned)
    "data_size_mb": [512],                 # buffer size in MB
    "repeat":       [5],                   # timing iterations
}

# ============================================================
# Data type metadata
# ============================================================
DTYPE_INFO = {
    0: {"name": "fp16",    "size": 2, "cce_type": "__fp16",  "host_type": "uint16_t"},
    1: {"name": "float32", "size": 4, "cce_type": "float",   "host_type": "float"},
    2: {"name": "int64",   "size": 8, "cce_type": "int64_t", "host_type": "int64_t"},
}

ACCESS_MODE_NAMES = {0: "read_only", 1: "write_only", 2: "read_write"}
SRC_MEMORY_NAMES  = {0: "GM", 1: "L2", 2: "UB"}

# ============================================================
# Paths
# ============================================================
SCRIPT_DIR       = Path(__file__).parent.resolve()
KERNEL_TEMPLATE  = SCRIPT_DIR / "kernel_template.cce"
HOST_SOURCE      = SCRIPT_DIR / "host_driver.cpp"
COMPILE_SCRIPT   = SCRIPT_DIR / "compile_bench.sh"
TEMP_KERNEL_SRC  = SCRIPT_DIR / "bench_kernel_src.cce"
HOST_BINARY      = SCRIPT_DIR / "host"
DEVICE_BINARY    = SCRIPT_DIR / "bench_kernel.o"
RESULTS_DIR      = SCRIPT_DIR / "results"


# ============================================================
# Kernel patching
# ============================================================
def lane_bits_to_mask(bits):
    """Convert bit count to LANE_MASK binary literal string."""
    return f"0b{'1' * bits}"


def patch_kernel(template_path, output_path, params):
    """Patch kernel template with benchmark parameters."""
    with open(template_path, "r") as f:
        content = f.read()

    lane_mask_str = lane_bits_to_mask(params["lane_bits"])
    # stride_factor > 0: relative mode (STRIDE = factor * lanes)
    # stride_factor == 0: absolute mode (use stride value directly)
    if params["stride_factor"] > 0:
        stride_abs = params["stride_factor"] * (1 << params["lane_bits"])
    else:
        stride_abs = params["stride"]

    patches = {
        "DATA_TYPE_ENUM": str(params["data_type"]),
        "SRC_MEMORY":     str(params["src_memory"]),
        "ACCESS_MODE":    str(params["access_mode"]),
        "THREAD_NUM":     str(params["thread_num"]),
        "LANE_MASK":      lane_mask_str,
        "BITS_MOVE":      str(params["lane_bits"]),
        "UNROLL_LOOP":    str(params["unroll_loop"]),
        "STRIDE":         str(stride_abs),
        "ALIGNMENT_OFFSET": str(params["align_offset"]),
    }

    for macro, value in patches.items():
        # Match: #define MACRO <anything>
        pattern = rf'(#define\s+{macro}\s+)\S+'
        replacement = rf'\g<1>{value}'
        content, count = re.subn(pattern, replacement, content)
        if count == 0:
            print(f"  [WARN] Macro {macro} not found in template")

    with open(output_path, "w") as f:
        f.write(content)


# ============================================================
# Build
# ============================================================
def compile_kernel(dtype_enum):
    """Compile kernel and host using compile_bench.sh."""
    cmd = ["bash", str(COMPILE_SCRIPT), str(TEMP_KERNEL_SRC), str(HOST_SOURCE), str(dtype_enum)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        print(f"  [ERROR] Compilation failed:")
        print(f"    stdout: {result.stdout[-500:]}")
        print(f"    stderr: {result.stderr[-500:]}")
        return False
    return True


# ============================================================
# Run
# ============================================================
def run_benchmark(block_num, data_size_mb, repeat, device_id=0, use_msprof=False):
    """Run the benchmark binary and return parsed result."""
    cmd = [str(HOST_BINARY), str(data_size_mb), str(block_num), str(repeat), str(device_id)]

    if use_msprof:
        prof_dir = str(RESULTS_DIR / "msprof")
        cmd = [
            "msprof",
            f"--output={prof_dir}",
            "--task-time=l1",
            "--aic-mode=task-based",
        ] + cmd

    try:
        result = subprocess.run(cmd, text=True, timeout=300,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except subprocess.TimeoutExpired:
        print("  [ERROR] Benchmark timed out")
        return None

    if result.returncode != 0:
        print(f"  [ERROR] Benchmark failed (exit code {result.returncode})")
        print(f"    output: {result.stdout[-300:]}")
        return None

    # Parse BANDWIDTH_RESULT line
    for line in result.stdout.splitlines():
        if line.startswith("BANDWIDTH_RESULT,"):
            parts = line.strip().split(",")
            if len(parts) >= 4:
                return {
                    "bytes_moved": float(parts[1]),
                    "time_us":     int(parts[2]),
                    "bandwidth_gbs": float(parts[3]),
                }
    print("  [WARN] Could not parse BANDWIDTH_RESULT from output")
    print(f"    output: {result.stdout[-300:]}")
    return None


# ============================================================
# Result collection
# ============================================================
def config_to_row(params, result):
    """Convert a parameter config + result into a CSV row dict."""
    if params["stride_factor"] > 0:
        stride_actual = params["stride_factor"] * (1 << params["lane_bits"])
    else:
        stride_actual = params["stride"]

    row = {
        "timestamp":       datetime.now().isoformat(),
        "src_memory":      params["src_memory"],
        "src_memory_name": SRC_MEMORY_NAMES.get(params["src_memory"], "?"),
        "data_type":       params["data_type"],
        "data_type_name":  DTYPE_INFO[params["data_type"]]["name"],
        "dtype_size":      DTYPE_INFO[params["data_type"]]["size"],
        "access_mode":     params["access_mode"],
        "access_mode_name": ACCESS_MODE_NAMES.get(params["access_mode"], "?"),
        "stride_factor":   params["stride_factor"],
        "stride":          stride_actual,
        "lane_bits":       params["lane_bits"],
        "lane_mask":       lane_bits_to_mask(params["lane_bits"]),
        "lanes":           1 << params["lane_bits"],
        "thread_num":      params["thread_num"],
        "block_num":       params["block_num"],
        "align_offset":    params["align_offset"],
        "data_size_mb":    params["data_size_mb"],
        "repeat":          params["repeat"],
    }
    if result:
        row["bytes_moved"]    = result["bytes_moved"]
        row["time_us"]        = result["time_us"]
        row["bandwidth_gbs"]  = result["bandwidth_gbs"]
    else:
        row["bytes_moved"]    = ""
        row["time_us"]        = ""
        row["bandwidth_gbs"]  = ""
    return row


CSV_COLUMNS = [
    "timestamp", "src_memory", "src_memory_name",
    "data_type", "data_type_name", "dtype_size",
    "access_mode", "access_mode_name",
    "stride_factor", "stride", "lane_bits", "lane_mask", "lanes",
    "thread_num", "block_num",
    "align_offset", "data_size_mb", "repeat",
    "bytes_moved", "time_us", "bandwidth_gbs",
]


def write_csv(rows, path):
    """Write results to CSV file."""
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


# ============================================================
# Main sweep logic
# ============================================================
def generate_configs(sweep_config, selected_factors=None):
    """Generate all parameter combinations from sweep config."""
    factors = {}
    for key, values in sweep_config.items():
        if selected_factors and key not in selected_factors:
            factors[key] = [values[0]] if values else [DEFAULT_SWEEP[key][0]]
        else:
            factors[key] = values

    keys = list(factors.keys())
    value_lists = [factors[k] for k in keys]

    configs = []
    for combo in product(*value_lists):
        config = dict(zip(keys, combo))
        # Skip invalid: thread_num must >= lanes (2^lane_bits)
        lanes = 1 << config["lane_bits"]
        if config["thread_num"] < lanes:
            continue
        configs.append(config)
    return configs


def dedup_compile_configs(configs):
    """
    Group configs by compile-time parameters.
    Only data_type, src_memory, access_mode, stride, lane_bits,
    thread_num, align_offset affect compilation.
    Host-side params (block_num, data_size_mb, repeat) don't need recompilation.
    """
    groups = {}
    for config in configs:
        compile_key = (
            config["data_type"],
            config["src_memory"],
            config["access_mode"],
            config["stride_factor"],
            config["stride"],
            config["lane_bits"],
            config["unroll_loop"],
            config["thread_num"],
            config["align_offset"],
        )
        if compile_key not in groups:
            groups[compile_key] = []
        groups[compile_key].append(config)
    return groups


def run_sweep(sweep_config, selected_factors=None, use_msprof=False, device_id=0):
    """Execute the full parameter sweep."""
    configs = generate_configs(sweep_config, selected_factors)
    compile_groups = dedup_compile_configs(configs)

    total = len(configs)
    print(f"Total test configurations: {total}")
    print(f"Unique compilations needed: {len(compile_groups)}")
    print()

    # Ensure results dir exists
    RESULTS_DIR.mkdir(exist_ok=True)

    all_rows = []
    test_num = 0

    for compile_key, group_configs in compile_groups.items():
        # Use first config in group for compilation
        representative = group_configs[0]

        dtype_enum = representative["data_type"]
        dtype_name = DTYPE_INFO[dtype_enum]["name"]
        access_name = ACCESS_MODE_NAMES.get(representative["access_mode"], "?")

        print("=" * 64)
        print(f"Compile: dtype={dtype_name} access={access_name} "
              f"stride_factor={representative['stride_factor']} lanes={1 << representative['lane_bits']} "
              f"threads={representative['thread_num']} align={representative['align_offset']}")
        print("=" * 64)

        # Patch and compile
        patch_kernel(KERNEL_TEMPLATE, TEMP_KERNEL_SRC, representative)
        if not compile_kernel(dtype_enum):
            print("  Skipping group due to compilation failure")
            for cfg in group_configs:
                test_num += 1
                all_rows.append(config_to_row(cfg, None))
            continue

        # Run each config in this compile group
        for cfg in group_configs:
            test_num += 1
            print(f"\n[{test_num}/{total}] "
                  f"blocks={cfg['block_num']} size={cfg['data_size_mb']}MB "
                  f"repeat={cfg['repeat']}")

            result = run_benchmark(
                block_num=cfg["block_num"],
                data_size_mb=cfg["data_size_mb"],
                repeat=cfg["repeat"],
                device_id=device_id,
                use_msprof=use_msprof,
            )

            row = config_to_row(cfg, result)
            all_rows.append(row)

            if result:
                print(f"  => {result['bandwidth_gbs']:.2f} GB/s "
                      f"({result['time_us']} us)")
            else:
                print(f"  => FAILED")

    return all_rows


# ============================================================
# Preset sweep configurations
# ============================================================
PRESETS = {
    "full": {
        "src_memory":   [0, 1],
        "data_type":    [0, 1, 2],
        "access_mode":  [0, 1, 2],
        "stride_factor": [1],
        "stride":       [0],
        "lane_bits":    [3, 5, 7, 9],
        "unroll_loop":  [4],
        "thread_num":   [512, 1024],
        "block_num":    [32, 64, 128],
        "align_offset": [0],
        "data_size_mb": [512],
        "repeat":       [5],
    },
    "dtype_sweep": {
        "src_memory":   [0],
        "data_type":    [0, 1, 2],
        "access_mode":  [0],
        "stride_factor": [1],
        "stride":       [0],
        "lane_bits":    [9],
        "unroll_loop":  [4],
        "thread_num":   [1024],
        "block_num":    [64],
        "align_offset": [0],
        "data_size_mb": [512],
        "repeat":       [5],
    },
    "burst_sweep": {
        "src_memory":   [0],
        "data_type":    [0],
        "access_mode":  [0],
        "stride_factor": [1],
        "stride":       [0],
        "lane_bits":    [3, 4, 5, 6, 7, 8, 9],
        "unroll_loop":  [4],
        "thread_num":   [1024],
        "block_num":    [64],
        "align_offset": [0],
        "data_size_mb": [512],
        "repeat":       [5],
    },
    "block_sweep": {
        "src_memory":   [0],
        "data_type":    [0],
        "access_mode":  [0],
        "stride_factor": [1],
        "stride":       [0],
        "lane_bits":    [9],
        "unroll_loop":  [4],
        "thread_num":   [1024],
        "block_num":    [1, 2, 4, 8, 16, 32, 64, 128],
        "align_offset": [0],
        "data_size_mb": [512],
        "repeat":       [5],
    },
    "access_sweep": {
        "src_memory":   [0],
        "data_type":    [0],
        "access_mode":  [0, 1, 2],
        "stride_factor": [1],
        "stride":       [0],
        "lane_bits":    [9],
        "unroll_loop":  [4],
        "thread_num":   [1024],
        "block_num":    [64],
        "align_offset": [0],
        "data_size_mb": [512],
        "repeat":       [5],
    },
    "align_sweep": {
        "src_memory":   [0],
        "data_type":    [0],
        "access_mode":  [0],
        "stride_factor": [1],
        "stride":       [0],
        "lane_bits":    [9],
        "unroll_loop":  [4],
        "thread_num":   [1024],
        "block_num":    [64],
        "align_offset": [0, 1, 2, 4, 8, 16],
        "data_size_mb": [512],
        "repeat":       [5],
    },
    "stride_sweep": {
        "src_memory":   [0],
        "data_type":    [0],
        "access_mode":  [0],
        "stride_factor": [1, 2, 4, 8, 16, 32],
        "stride":       [0],
        "lane_bits":    [9],
        "unroll_loop":  [4],
        "thread_num":   [1024],
        "block_num":    [64],
        "align_offset": [0],
        "data_size_mb": [512],
        "repeat":       [5],
    },
    "thread_sweep": {
        "src_memory":   [0],
        "data_type":    [0],
        "access_mode":  [0],
        "stride_factor": [1],
        "stride":       [0],
        "lane_bits":    [9],
        "unroll_loop":  [4],
        "thread_num":   [64, 128, 256, 512, 1024],
        "block_num":    [64],
        "align_offset": [0],
        "data_size_mb": [512],
        "repeat":       [5],
    },
}


# ============================================================
# CLI
# ============================================================
def parse_args():
    parser = argparse.ArgumentParser(
        description="Ascend DV100 SIMT Bandwidth Benchmark Framework",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--preset", "-p",
        choices=list(PRESETS.keys()),
        default=None,
        help="Use a preset sweep configuration",
    )
    parser.add_argument(
        "--factors", "-f",
        nargs="+",
        default=None,
        help="Only sweep these factors (others use default single value). "
             "E.g.: --factors data_type block_num",
    )
    parser.add_argument(
        "--msprof",
        action="store_true",
        help="Run with msprof profiling",
    )
    parser.add_argument(
        "--device", "-d",
        type=int,
        default=0,
        help="Ascend device ID (default: 0)",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="Output CSV file path (default: results/benchmark_<timestamp>.csv)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be tested without executing",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # Select sweep config
    if args.preset:
        sweep_config = copy.deepcopy(PRESETS[args.preset])
        print(f"Using preset: {args.preset}")
    else:
        sweep_config = copy.deepcopy(DEFAULT_SWEEP)

    # Generate configs for dry-run
    configs = generate_configs(sweep_config, args.factors)
    compile_groups = dedup_compile_configs(configs)

    if args.dry_run:
        print(f"\n{'='*64}")
        print(f"DRY RUN - {len(configs)} configurations, {len(compile_groups)} compilations")
        print(f"{'='*64}\n")
        for i, cfg in enumerate(configs):
            dtype_name = DTYPE_INFO[cfg["data_type"]]["name"]
            access_name = ACCESS_MODE_NAMES.get(cfg["access_mode"], "?")
            src_name = SRC_MEMORY_NAMES.get(cfg["src_memory"], "?")
            if cfg["stride_factor"] > 0:
                stride_abs = cfg["stride_factor"] * (1 << cfg["lane_bits"])
                stride_desc = f"factor={cfg['stride_factor']}({stride_abs}el)"
            else:
                stride_abs = cfg["stride"]
                stride_desc = f"abs={stride_abs}el"
            print(f"  [{i+1:4d}] {src_name:>3s} {dtype_name:>7s} {access_name:>10s} "
                  f"stride=[{stride_desc}] "
                  f"lanes={1<<cfg['lane_bits']:>3d} "
                  f"threads={cfg['thread_num']:>4d} blocks={cfg['block_num']:>3d} "
                  f"align={cfg['align_offset']} size={cfg['data_size_mb']}MB")
        return

    # Run sweep
    print(f"\nStarting benchmark sweep...")
    print(f"  Configurations: {len(configs)}")
    print(f"  Compilations:   {len(compile_groups)}")
    print(f"  Device:         {args.device}")
    print(f"  msprof:         {args.msprof}")
    print()

    start_time = time.time()
    rows = run_sweep(sweep_config, args.factors, args.msprof, args.device)
    elapsed = time.time() - start_time

    # Write results
    if args.output:
        csv_path = Path(args.output)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = RESULTS_DIR / f"benchmark_{timestamp}.csv"

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    write_csv(rows, csv_path)

    # Summary
    print(f"\n{'='*64}")
    print(f"Benchmark sweep complete!")
    print(f"  Total time:  {elapsed:.1f}s")
    print(f"  Results:     {len(rows)} rows")
    successful = [r for r in rows if r.get("bandwidth_gbs")]
    failed = len(rows) - len(successful)
    print(f"  Successful:  {len(successful)}")
    print(f"  Failed:      {failed}")
    print(f"  Output CSV:  {csv_path}")

    if successful:
        bws = [r["bandwidth_gbs"] for r in successful]
        print(f"\n  Bandwidth stats:")
        print(f"    Min:  {min(bws):.2f} GB/s")
        print(f"    Max:  {max(bws):.2f} GB/s")
        print(f"    Mean: {sum(bws)/len(bws):.2f} GB/s")
    print(f"{'='*64}")


if __name__ == "__main__":
    main()
