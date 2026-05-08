#!/bin/bash
# =============================================================================
# run_sweep.sh
# 遍历 LANE_MASK (0b111~0b11111111) 与 BITS_MOVE (3~8)，
# 每次修改源码 → 编译 → msprof 采集 → 最终汇总 CSV
# =============================================================================

set -euo pipefail

# --------------------------------------------------------------------------- #
# 用户可调参数
# --------------------------------------------------------------------------- #
SOURCE_FILE="dv100_simt_bandwidth_test.cce"          # 含 LANE_MASK / BITS_MOVE 宏的源文件
RESULTS_BASE="/home/w00951285/ttk-master/simt_bandwidth/results-burstlen"
MSPROF_CMD="msprof"
HOST_CMD="./host"
MERGED_CSV="merged_op_summary.csv"

# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #

# 将 BITS_MOVE 值转成对应的 LANE_MASK 二进制字面量字符串，例如 3 → 0b111
bits_to_mask() {
    local bits=$1
    local mask=""
    for (( j=0; j<bits; j++ )); do
        mask="1${mask}"
    done
    echo "0b${mask}"
}

# 在 SOURCE_FILE 里原地替换两条 #define
patch_source() {
    local lane_mask_str=$1   # e.g. "0b11111111"
    local bits_move=$2       # e.g. 8

    # 替换 LANE_MASK 行（保留注释）
    sed -i "s|^#define LANE_MASK .*|#define LANE_MASK ${lane_mask_str}|" "${SOURCE_FILE}"
    # 替换 BITS_MOVE 行
    sed -i "s|^#define BITS_MOVE .*|#define BITS_MOVE ${bits_move}|"     "${SOURCE_FILE}"
}

# --------------------------------------------------------------------------- #
# 主循环
# --------------------------------------------------------------------------- #
header_written=false

for bits in $(seq 3 9); do

    lane_mask_str=$(bits_to_mask "${bits}")
    echo "============================================================"
    echo "  BITS_MOVE=${bits}  LANE_MASK=${lane_mask_str}"
    echo "============================================================"

    # 1. 修改源文件
    patch_source "${lane_mask_str}" "${bits}"

    # 2. 编译
    echo "[$(date '+%H:%M:%S')] Compiling..."
    bash compile.sh
    echo "[$(date '+%H:%M:%S')] Compile OK"

    # 3. 记录 profiling 开始前已有的子目录，用于后续定位新产生的目录
    existing_dirs=$(ls -d "${RESULTS_BASE}"/PROF_* 2>/dev/null || true)

    # 4. 运行 msprof
    echo "[$(date '+%H:%M:%S')] Running msprof..."
    ${MSPROF_CMD} \
        --output="${RESULTS_BASE}" \
        --task-time=l1 \
        --aic-mode=task-based \
        ${HOST_CMD}
    echo "[$(date '+%H:%M:%S')] msprof finished"

    # 5. 找到本次新生成的 PROF_* 目录（取最新的一个）
    new_prof_dir=$(ls -dt "${RESULTS_BASE}"/PROF_* 2>/dev/null | head -1 || true)
    if [[ -z "${new_prof_dir}" ]]; then
        echo "  [WARN] 未找到 PROF_* 目录，跳过 CSV 收集" >&2
        continue
    fi
    echo "  PROF dir: ${new_prof_dir}"

    # 6. 在 mindstudio_profiler_output/ 下找 op_summary_*.csv
    csv_file=$(find "${new_prof_dir}/mindstudio_profiler_output" \
                    -maxdepth 1 -name "op_summary_*.csv" 2>/dev/null \
               | sort | tail -1 || true)

    if [[ -z "${csv_file}" ]]; then
        echo "  [WARN] 未找到 op_summary CSV，跳过" >&2
        continue
    fi
    echo "  CSV: ${csv_file}"

    # 7. 追加到合并文件
    #    第一次写入时保留表头；之后只取数据行（跳过第一行）
    if [[ "${header_written}" == false ]]; then
        # 写表头 + 追加两列说明来自哪个配置
        head -1 "${csv_file}" | \
            awk -F',' -v OFS=',' '{print "LANE_MASK,BITS_MOVE," $0}' \
            > "${MERGED_CSV}"
        header_written=true
    fi

    # 写数据行（跳过表头），在每行前插入配置标签
    tail -n +2 "${csv_file}" | \
        awk -F',' -v OFS=',' \
            -v lm="${lane_mask_str}" \
            -v bm="${bits}" \
        '{print lm, bm, $0}' \
        >> "${MERGED_CSV}"

    echo "  已追加到 ${MERGED_CSV}"
done

echo ""
echo "============================================================"
echo " 全部扫描完成！合并结果已保存到："
echo "   ${MERGED_CSV}"
echo "============================================================"