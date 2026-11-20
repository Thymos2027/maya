# 本机 Windows 构建环境

- Visual Studio 2022 Build Tools 已安装在 `D:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools`。
- 可用的 MSVC 工具集目录为 `D:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC\14.42.34433`，环境初始化脚本为 `D:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat`。
- Windows SDK 安装在 `C:\Program Files (x86)\Windows Kits\10`。
- Maya 默认通过 `%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe` 查找 Visual Studio；本机该默认位置没有 `vswhere.exe`，因此自动探测可能误报未安装。不要重装 Build Tools；应优先让 Maya 的探测逻辑回退到上述固定 `vcvars64.bat`。
- 全局 CUDA Toolkit 使用 12.8.1，标准安装目录为 `C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8`；为 RTX 5060 Ti、VS 2022 和本项目优先使用该版本，除非后续项目要求发生变化。
- 检查 CUDA 环境时不能只确认 `nvcc.exe`：还必须确认 `include\cublas_v2.h`、`lib\x64\cublas.lib` 和 `lib\x64\cublasLt.lib` 存在。构建 Maya 时优先显式传入 `--nvcc "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8\bin\nvcc.exe"`。
- `E:\Coder\LLM\llm-inference-lab\.local\cuda-13.3` 是其他项目的局部精简环境，缺少 cuBLAS 开发文件，不能用于构建 Maya。
