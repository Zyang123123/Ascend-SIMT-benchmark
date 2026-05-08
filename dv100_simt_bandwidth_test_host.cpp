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
#include <sys/time.h>

// #define DTYPE int32_t
// #define DTYPE int16_t
// #define DTYPE float
#define DTYPE uint16_t

using namespace std;
using Int64Vector2D = std::vector<std::vector<int64_t>>;
using FloatVector2D = std::vector<std::vector<float>>;



#define EXPECT_EQ(a, b)                                                     \
do {                                                                        \
    if (a != b) {                                                           \
        fprintf(stderr, "Assertion failed at %s:%d\n", __FILE__, __LINE__); \
        fprintf(stderr, "  Expected: %d\n", b);                             \
        fprintf(stderr, "  Actual: %d\n", a);                               \
        fprintf(stderr, "  Function: %s\n", __func__);                      \
        abort();                                                            \
    }                                                                       \
} while (0)

static unsigned long currentTime()
{
    struct timeval tv;

    gettimeofday(&tv, NULL);

    return (tv.tv_sec * 1000000 + tv.tv_usec);
}

char *readBinFile(const char *file_name, uint32_t *fileSize)
{
    std::filebuf *pbuf;
    std::ifstream filestr;
    size_t size;
    filestr.open(file_name, std::ios::binary);
    if (!filestr) {
        return (char *)("cannot open file ");
        return NULL;
    }
    pbuf = filestr.rdbuf();
    size = pbuf->pubseekoff(0, std::ios::end, std::ios::in);
    pbuf->pubseekpos(0, std::ios::in);
    char *buffer = new char[size];
    if (NULL == buffer) {
        return (char *)("cannot malloc buffer size");
        return NULL;
    }
    pbuf->sgetn(buffer, size);
    *fileSize = size;
    filestr.close();
    return buffer;
}

std::string RegisterBinaryKernel(const char *func_name, const char *bin_file, char **buffer)
{
    rtDevBinary_t binary;
    void *binHandle = NULL;

    uint32_t bufferSize = 0;
    *buffer = readBinFile(bin_file, &bufferSize);
    if (NULL == *buffer) {
        printf("readBinFile failed\n");
        return "readBinFile failed";
    }

    binary.data = *buffer;
    binary.length = bufferSize;

    binary.magic = RT_DEV_BINARY_MAGIC_ELF_AIVEC;
    binary.version = 0;
    rtError_t rtRet = rtDevBinaryRegister(&binary, &binHandle);
    if (rtRet != RT_ERROR_NONE) {
        printf("rtDevBinaryRegister failed: %d.\n", rtRet);
        return "rtDevBinaryRegister failed!";
    }
    rtRet = rtFunctionRegister(binHandle, func_name, func_name, (void *)func_name, 0);
    if (rtRet != RT_ERROR_NONE) {
        printf("rtFunctionRegister failed: %d\n", rtRet);
        return "rtFunctionRegister failed!";
    }
    return "true";
}

void kernel_and_event(const char *func_name, const char *bin_file)
{
    const int64_t test_size = (512ULL*1024*1024)/sizeof(DTYPE);
    const int64_t repeat = 1;
    rtStream_t stream;
    rtError_t error;

    char *buffer = nullptr;
    std::string ret = RegisterBinaryKernel(func_name, bin_file, &buffer);
    if (ret != "true") {
        printf("RegisterBinaryKernel Failed!\n");
        return;
    }

    error = rtStreamCreate(&stream, 0);
    EXPECT_EQ(error, RT_ERROR_NONE);

    uint16_t ModuleId = 0;
    // read data
    void *x0_hbm = NULL;
    void *y0_hbm = NULL;
    error = rtMalloc((void **)&x0_hbm, test_size * sizeof(DTYPE), RT_MEMORY_HBM, ModuleId);
    EXPECT_EQ(error, RT_ERROR_NONE);
    error = rtMalloc((void **)&y0_hbm, test_size * sizeof(DTYPE), RT_MEMORY_HBM, ModuleId);
    EXPECT_EQ(error, RT_ERROR_NONE);

    // read input
    // __fp16 不支持直接从 std::rand() 隐式转换，先生成 float 再转为 DTYPE
    std::vector<DTYPE> x0_data(test_size);

    std::srand(std::time(nullptr));
    std::generate_n(x0_data.begin(), x0_data.size(),
        [&](){ return static_cast<DTYPE>(float(std::rand() % 100) / 100.f); });

    error = rtMemcpy(x0_hbm, test_size * sizeof(DTYPE),
        x0_data.data(), test_size * sizeof(DTYPE), RT_MEMCPY_HOST_TO_DEVICE);
    EXPECT_EQ(error, RT_ERROR_NONE);


    struct KernelArgs {
        void *input_device;
        void *output_device;
        int64_t test_size;
        int64_t repeat;
    } kernel_args;

    uint32_t blockDim = 64;  // notice

    kernel_args.input_device = x0_hbm;
    kernel_args.output_device = y0_hbm;
    kernel_args.test_size = test_size;
    kernel_args.repeat = repeat;

    rtArgsEx_t argsInfo = {};
    argsInfo.args = static_cast<void*>(&kernel_args);
    argsInfo.argsSize = sizeof(kernel_args);
    rtTaskCfgInfo_t cfgInfo = {};
    cfgInfo.localMemorySize = 128 * 1024;

    error = rtKernelLaunchWithFlagV2((void *)func_name, blockDim, &argsInfo, NULL, stream, 0, &cfgInfo);
    EXPECT_EQ(error, RT_ERROR_NONE);

    unsigned long long startTime, endTime;
    int iters = 5;
    startTime = currentTime();

    for (int profile_repeat_i = 0; profile_repeat_i < iters; ++profile_repeat_i) {
      error = rtKernelLaunchWithFlagV2((void *)func_name, blockDim, &argsInfo, NULL, stream, 0, &cfgInfo);
      EXPECT_EQ(error, RT_ERROR_NONE);
    }

    error = rtStreamSynchronize(stream);
    EXPECT_EQ(error, RT_ERROR_NONE);
    endTime = currentTime();
    unsigned long long duration = (endTime - startTime) / iters;
    cout << "Time cost : " <<  duration / 1000.0 << " ms" << endl;
    cout << "Bandwidth: " << test_size * sizeof(DTYPE) * repeat * (1000000) / duration / 1024 / 1024 / 1024 << " GB/s" << endl;

    cout << "kernel_and_event end. "<<endl;
}

int main(int argc, char *argv[])
{
    aclInit(nullptr);
    int32_t device_cnt = 0;
    rtError_t error;
    error = rtGetDeviceCount(&device_cnt);
    printf("device cnt = %d\n", device_cnt);
    int32_t device_id = 0;
    error = rtSetDevice(device_id);
    if (error) {
        printf("set device: error=%d\n", error);
        return 0;
    } else {
        printf("##### set device %d success ####\n\n", device_id);
    }

    std::string func = "dv100_bandwidth_test";  // kernel func
    std::string bin_file = "dv100_simt_bandwidth_test.o";
    kernel_and_event(func.c_str(), bin_file.c_str());

    error = rtDeviceReset(device_id);
    EXPECT_EQ(error, RT_ERROR_NONE);

    aclFinalize();

    return 0;
}