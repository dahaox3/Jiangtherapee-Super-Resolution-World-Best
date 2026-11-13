# JSR 主摄助手

给 iQOO 12 Pro（V2329A）1× 主摄增加简单的七张合成入口。
每批照片自动读取 ISO、快门和 DNG NoiseProfile，再计算并传给 JSR 三参数。
**只要求同一组七张的 ISO 和快门相同；不同组可以使用不同的 ISO、快门。**

## 日常使用

1. 用同一个相机 App 的 1× 主摄拍七张同一场景的原始 DNG，固定本组的 ISO、快门和对焦。当前支持 4096×3072 Bayer DNG 模式。
2. 双击 `JSR-Easy.exe`，点“选择 7 张 DNG”。程序自动核对七张照片并显示拍摄设置。
3. 点“合成”。完成后点“打开结果”或“打开输出目录”。

无需选择 PTC、填写三个数字或逐次确认等曝光。参数每批重新读取；没有写死 ISO 400、1/50 秒或某组标定数值。
如果照片混入另一档 ISO、快门、长焦镜头，或缺少噪声模型，程序会指出具体文件与原因。
噪声模型及通道值必须一致，避免把一组标定错误应用到不同模式。

默认输出在照片文件夹的 `JSR_Easy_Output` 下，每次合成创建独立目录。
也可以改成 D 盘目录，程序会记住上次路径。每次输出带 `job.json` 和 `engine.log`，便于查明参数与失败原因。
应用设置和引擎临时文件在助手旁的 `UserData`。安装在 D 盘时这些文件也留在 D 盘。
原片仅以只读方式访问，输出不会覆盖原片。

## 安装便携版

下载并完整解压 [官方 JSR V18.3.1](https://github.com/y-g-jiang/Jiangtherapee-Super-Resolution-World-Best/releases/tag/jsr-v18.3.1)，保留 resources 文件夹。
将本 fork 的 `JSR-Easy` 文件夹放在它旁边，例如：

```text
D:\camera_super_resolution\
  JSR_V18.3.1\resources\jsr_backend.exe
  JSR-Easy\JSR-Easy.exe
```

助手会自动找到旁边的引擎；不在旁边时，展开“合成引擎设置”选择官方包的 `resources/jsr_backend.exe`。
当前只验证并接受 JSR V18.3.1 内对应 SHA-256 的七张引擎。不同版本需重新验证接口。
便携包自带助手需要的 Python/Tk 运行时；不需要另外安装 Python、CUDA 或 pip 依赖。

## 参数来源和范围

黑电平来自 DNG 的 BlackLevel。NoiseProfile 的方差模型为 `S*x + O`，x 是扣黑后按白电平归一化的信号。
助手根据 `1/S` 估算当前模式下的有效满阱电子数，按 `(WhiteLevel-BlackLevel)*sqrt(O)` 估算读噪 DN。
这些是手机写入的噪声模型估计，不是独立测量的物理满阱。文件缺少或存在无法可靠处理的模型时不会猜值。
标准依据：[Android Camera2 NoiseProfile](https://developer.android.com/reference/android/hardware/camera2/CaptureResult#SENSOR_NOISE_PROFILE)、[Adobe DNG 规范](https://helpx.adobe.com/camera-raw/desktop/dng-and-file-formats/digital-negative.html)。

仍使用官方七张 WGPU 引擎，默认输出 2× 线性 RGB DNG，适合静态场景；不启用额外去鬼影。
默认曝光标签、色彩处理和重建算法沿用该引擎。界面自动化不等于提升传感器标定精度。
完整 Windows 界面和当前后端的源码未在上游仓库公开，因此本 fork 新增独立入口调用原引擎，没有重建完整 JSR 源码。

## 从源码运行与构建

运行助手只需带 Tk 的 Python 3.12 或更新版本，无第三方运行依赖：

```powershell
py -3.12 -m iqoo_easy
py -3.12 -m unittest discover -s tests -v
```

打包 Windows 便携版（默认构建缓存、虚拟环境和输出都在仓库目录）：

```powershell
.\build-windows.ps1
```

支持指定 D 盘构建路径：

```powershell
.\build-windows.ps1 -BuildRoot D:\camera_super_resolution\.build -OutputRoot D:\camera_super_resolution\dist
```

开发者也可以使用 `python -m iqoo_easy inspect <七个 DNG 路径> --report check.json`。
批处理使用 `python -m iqoo_easy run <七个 DNG 路径> --output-dir <输出目录> --report result.json`。
窗口版 EXE 支持同样参数，配合 `--report` 读取结果。
测试使用合成元数据与假的子进程引擎，不在仓库内保存个人照片。

作者和组件许可见 [NOTICE-EASY.md](NOTICE-EASY.md)。
