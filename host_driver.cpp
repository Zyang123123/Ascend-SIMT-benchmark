// ============================================================
// Ascend DV100 SIMT Bandwidth Benchmark - Host Driver
// ============================================================
// Usage: ./host [data_size_mb] [block_num] [repeat] [device_id]
//   data_size_mb : total buffer size in MB (default 512)
//   block_num    : number of blocks to launch (default 64)
//   repeat       : kernel repeat count (default 5)
//   device_id    : Ascend device ID (default 0)
//
// Output format (stdout, one line per metric):
//   BANDWIDTH_RESULT,<bytes_moved>,<time_us>,<bandwidth_gbs>,<data_type_enum>,<access_mode>
// ============================================================

#include <acl/acl.h>
#include "experiment/runtime/runtime/rt.h"

#include <string>
#include <vector>
#include <fstream>
#include <iostream>
#include <cassert>
#include <cstdlib>
#include <algorithm>
#include <cmath>
#include <ctime>
#include <cstring>
#include <sys/time.h>

// Match kernel's DATA_TYPE_ENUM for host-side allocation
// 0=fp16(uint16_t), 1=float32, 2=int64
#ifndef HOST_DTYPE_ENUM
#define HOST_DTYPE_ENUM 0
#endif

#if HOST_DTYPE_ENUM == 0
  typedef uint16_t host_dtype;
  #define HOST_DTYPE_SIZE 2
#elif HOST_DTYPE_ENUM == 1
  typedef float host_dtype;
  #define HOST_DTYPE_SIZE 4
#elif HOST_DTYPE_ENUM == 2
  typedef int64_t host_dtype;
  #define HOST_DTYPE_SIZE 8
#else
  #error "Unsupported HOST_DTYPE_ENUM"
#endif

using namespace std;

#define EXPECT_EQ(a, b)                                                     \
do {                                                                        \
    if (a != b) {                                                           \
        fprintf(stderr, "Assertion failed at %s:%d\n", __FILE__, __LINE__); \
        fprintf(stderr, "  Expected: %d\n", (int)(b));                      \
        fprintf(stderr, "  Actual: %d\n", (int)(a));                        \
        fprintf(stderr, "  Function: %s\n", __func__);                      \
        abort();                                                            \
    }                                                                       \
} while (0)

static unsigned long currentTime() {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return tv.tv_sec * 1000000 + tv.tv_usec;
}

char *readBinFile(const char *file_name, uint32_t *fileSize) {
    std::filebuf *pbuf;
    std::ifstream filestr;
    size_t size;
    filestr.open(file_name, std::ios::binary);
    if (!filestr) {
        return NULL;
    }
    pbuf = filestr.rdbuf();
    size = pbuf->pubseekoff(0, std::ios::end, std::ios::in);
    pbuf->pubseekpos(0, std::ios::in);
    char *buffer = new char[size];
    if (NULL == buffer) {
        return NULL;
    }
    pbuf->sgetn(buffer, size);
    *fileSize = size;
    filestr.close();
    return buffer;
}

std::string RegisterBinaryKernel(const char *func_name, const char *bin_file, char **buffer) {
    rtDevBinary_t binary;
    void *binHandle = NULL;

    uint32_t bufferSize = 0;
    *buffer = readBinFile(bin_file, &bufferSize);
    if (NULL == *buffer) {
        return "readBinFile failed";
    }

    binary.data = *buffer;
    binary.length = bufferSize;
    binary.magic = RT_DEV_BINARY_MAGIC_ELF_AIVEC;
    binary.version = 0;
    rtError_t rtRet = rtDevBinaryRegister(&binary, &binHandle);
    if (rtRet != RT_ERROR_NONE) {
        return "rtDevBinaryRegister failed";
    }
    rtRet = rtFunctionRegister(binHandle, func_name, func_name, (void *)func_name, 0);
    if (rtRet != RT_ERROR_NONE) {
        return "rtFunctionRegister failed";
    }
    return "true";
}

void print_usage(const char *prog) {
    fprintf(stderr, "Usage: %s [data_size_mb] [block_num] [repeat] [device_id]\n", prog);
    fprintf(stderr, "  data_size_mb : buffer size in MB (default 512)\n");
    fprintf(stderr, "  block_num    : number of blocks (default 64)\n");
    fprintf(stderr, "  repeat       : kernel repeat iterations for timing (default 5)\n");
    fprintf(stderr, "  device_id    : Ascend device ID (default 0)\n");
}

void run_benchmark(int64_t data_size_mb, int block_num, int repeat, int device_id) {
    const char *func_name = "dv100_bandwidth_test";
    const char *bin_file = "bench_kernel.o";

    int64_t total_bytes = data_size_mb * 1024LL * 1024;
    int64_t element_count = total_bytes / HOST_DTYPE_SIZE;

    // Ensure 512B alignment
    int64_t aligned_elements = (element_count / (512 / HOST_DTYPE_SIZE)) * (512 / HOST_DTYPE_SIZE);
    if (aligned_elements == 0) aligned_elements = element_count;

    rtError_t error;
    rtStream_t stream;

    error = rtSetDevice(device_id);
    if (error) {
        fprintf(stderr, "rtSetDevice(%d) failed: %d\n", device_id, error);
        return;
    }

    char *buffer = nullptr;
    std::string ret = RegisterBinaryKernel(func_name, bin_file, &buffer);
    if (ret != "true") {
        fprintf(stderr, "RegisterBinaryKernel failed: %s\n", ret.c_str());
        return;
    }

    error = rtStreamCreate(&stream, 0);
    EXPECT_EQ(error, RT_ERROR_NONE);

    uint16_t ModuleId = 0;
    void *x0_hbm = NULL;
    void *y0_hbm = NULL;
    error = rtMalloc((void **)&x0_hbm, aligned_elements * HOST_DTYPE_SIZE, RT_MEMORY_HBM, ModuleId);
    EXPECT_EQ(error, RT_ERROR_NONE);
    error = rtMalloc((void **)&y0_hbm, aligned_elements * HOST_DTYPE_SIZE, RT_MEMORY_HBM, ModuleId);
    EXPECT_EQ(error, RT_ERROR_NONE);

    // Initialize input data
    std::vector<host_dtype> x0_data(aligned_elements);
    std::srand(std::time(nullptr));
    std::generate_n(x0_data.begin(), x0_data.size(),
        [](){ return static_cast<host_dtype>(float(std::rand() % 100) / 100.f); });

    error = rtMemcpy(x0_hbm, aligned_elements * HOST_DTYPE_SIZE,
        x0_data.data(), aligned_elements * HOST_DTYPE_SIZE, RT_MEMCPY_HOST_TO_DEVICE);
    EXPECT_EQ(error, RT_ERROR_NONE);

    struct KernelArgs {
        void *input_device;
        void *output_device;
        int64_t test_size;
        int64_t repeat;
    } kernel_args;

    kernel_args.input_device = x0_hbm;
    kernel_args.output_device = y0_hbm;
    kernel_args.test_size = aligned_elements;
    kernel_args.repeat = 1;  // repeat is handled inside the timed loop below

    rtArgsEx_t argsInfo = {};
    argsInfo.args = static_cast<void*>(&kernel_args);
    argsInfo.argsSize = sizeof(kernel_args);
    rtTaskCfgInfo_t cfgInfo = {};
    cfgInfo.localMemorySize = 128 * 1024;

    // Warmup launch
    error = rtKernelLaunchWithFlagV2((void *)func_name, block_num, &argsInfo, NULL, stream, 0, &cfgInfo);
    EXPECT_EQ(error, RT_ERROR_NONE);
    error = rtStreamSynchronize(stream);
    EXPECT_EQ(error, RT_ERROR_NONE);

    // Timed launches
    unsigned long long startTime, endTime;
    startTime = currentTime();

    for (int i = 0; i < repeat; ++i) {
        error = rtKernelLaunchWithFlagV2((void *)func_name, block_num, &argsInfo, NULL, stream, 0, &cfgInfo);
        EXPECT_EQ(error, RT_ERROR_NONE);
    }

    error = rtStreamSynchronize(stream);
    EXPECT_EQ(error, RT_ERROR_NONE);
    endTime = currentTime();

    unsigned long long duration_us = (endTime - startTime) / repeat;
    double bandwidth_gbs = (double)(aligned_elements * HOST_DTYPE_SIZE) * 1000000.0
                           / (double)duration_us / 1024.0 / 1024.0 / 1024.0;

    // Structured output for automated parsing
    printf("BANDWIDTH_RESULT,%.0f,%llu,%.3f,%d,%d\n",
           (double)(aligned_elements * HOST_DTYPE_SIZE),
           duration_us,
           bandwidth_gbs,
           HOST_DTYPE_ENUM,
           0);  // access_mode placeholder

    cout << "Time cost : " << duration_us / 1000.0 << " ms" << endl;
    cout << "Bandwidth : " << bandwidth_gbs << " GB/s" << endl;
    cout << "Elements  : " << aligned_elements << endl;
    cout << "Blocks    : " << block_num << endl;

    error = rtFree(x0_hbm);
    EXPECT_EQ(error, RT_ERROR_NONE);
    error = rtFree(y0_hbm);
    EXPECT_EQ(error, RT_ERROR_NONE);
    error = rtStreamDestroy(stream);
    EXPECT_EQ(error, RT_ERROR_NONE);

    delete[] buffer;
}

int main(int argc, char *argv[]) {
    if (argc >= 2 && (strcmp(argv[1], "-h") == 0 || strcmp(argv[1], "--help") == 0)) {
        print_usage(argv[0]);
        return 0;
    }

    int64_t data_size_mb = (argc > 1) ? atoll(argv[1]) : 512;
    int block_num        = (argc > 2) ? atoi(argv[2])  : 64;
    int repeat           = (argc > 3) ? atoi(argv[3])  : 5;
    int device_id        = (argc > 4) ? atoi(argv[4])  : 0;

    aclInit(nullptr);

    int32_t device_cnt = 0;
    rtError_t error = rtGetDeviceCount(&device_cnt);
    printf("device cnt = %d\n", device_cnt);

    run_benchmark(data_size_mb, block_num, repeat, device_id);

    error = rtDeviceReset(device_id);
    EXPECT_EQ(error, RT_ERROR_NONE);

    aclFinalize();
    return 0;
}
