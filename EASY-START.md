# JSR 主摄助手

给 iQOO 12 Pro（V2329A）1× 主摄增加简单的七张合成入口。
每批照片自动读取 ISO、快门和 DNG NoiseProfile，在修正版 JSR 引擎中逐通道标定。
**只要求同一组七张的 ISO 和快门相同；不同组可以使用不同的 ISO、快门。**

## 日常使用

1. 用同一个相机 App 的 1× 主摄拍七张同一场景的原始 DNG，固定本组的 ISO、快门和对焦。当前支持 4096×3072 Bayer DNG 模式。
2. 双击 `JSR-Easy.exe`，点“选择 7 张 DNG”。程序自动核对七张照片并显示拍摄设置。
3. 点“合成”。完成后点“打开结果”或“打开输出目录”。

无需选择 PTC、填写三个数字或逐次确认等曝光。参数每批重新读取；没有写死 ISO 400、1/50 秒或某组标定数值。
如果照片混入另一档 ISO、快门、长焦镜头，或缺少噪声模型，程序会指出具体文件与原因。
v0.2.0 将每张原片的四个黑电平和 NoiseProfile 分别传给原版引擎内部。
例如 `[64.06,64,64,64.06]` 会保留全部数值，不再取均值替代；同一组中各帧的黑电平、噪声模型也可以不同。
`calibration-cfa.json` 保留提交值，`CFA_CALIBRATION.json` 记录引擎实际使用的四相位参数。
取消了默认额外 +2 EV，DNG 缩略图与 PNG/JPEG 导出共用白平衡、颜色与显示处理。
Photoshop/Camera Raw 仍可能应用自身配置文件、曲线或已保存的编辑设置。

默认输出在照片文件夹的 `JSR_Easy_Output` 下，每次合成创建独立目录。
也可以改成 D 盘目录，程序会记住上次路径。每次输出带 `job.json` 和 `engine.log`，便于查明参数与失败原因。
应用设置和引擎临时文件在助手旁的 `UserData`。安装在 D 盘时这些文件也留在 D 盘。
原片仅以只读方式访问，输出不会覆盖原片。

## 安装便携版

使用已经保留的官方 JSR V18.3.1 便携版，完整解压并保留 resources 文件夹，再安装本 fork 的引擎补丁。
注意：[官方 18.3.1 发布页](https://github.com/y-g-jiang/Jiangtherapee-Super-Resolution-World-Best/releases/tag/jsr-v18.3.1) 的同名下载附件现已被作者替换为 18.2.1；文件名不能证明引擎版本，助手会校验实际 SHA-256。
将本 fork 的 `JSR-Easy` 文件夹放在它旁边，例如：

```text
D:\camera_super_resolution\
  JSR_V18.3.1\resources\jsr_backend.exe
  JSR-Easy\JSR-Easy.exe
```

助手会自动找到旁边的引擎；不在旁边时，展开“合成引擎设置”选择打过补丁的 `resources/jsr_backend.exe`。
v0.2.0 只接受对应修正版的校验值，原版或其他版本会被拒绝，防止逐通道参数被忽略。
补丁的原版输入 SHA-256：`45b520c43037a7e5033a6e08f97bb4972413440462eea5280986a7237f889729`。
修正版校验值固定在 `iqoo_easy/runner.py`。补丁不会覆盖备份或修改原片，安装方法见 [ENGINE-PATCH.md](ENGINE-PATCH.md)。
便携包自带助手需要的 Python/Tk 运行时；不需要另外安装 Python、CUDA 或 pip 依赖。

## 参数来源和范围

黑电平来自 DNG 的 BlackLevel。NoiseProfile 的方差模型为 `S*x + O`，x 是扣黑后按白电平归一化的信号。
助手根据 `1/S` 估算当前模式下的有效满阱电子数，按 `(WhiteLevel-BlackLevel)*sqrt(O)` 估算读噪 DN。
这些是手机写入的噪声模型估计，不是独立测量的物理满阱。文件缺少或存在无法可靠处理的模型时不会猜值。
标准依据：[Android Camera2 NoiseProfile](https://developer.android.com/reference/android/hardware/camera2/CaptureResult#SENSOR_NOISE_PROFILE)、[Adobe DNG 规范](https://helpx.adobe.com/camera-raw/desktop/dng-and-file-formats/digital-negative.html)。

仍使用官方七张 WGPU 重建与模型，默认输出 2× 线性 RGB DNG，适合静态场景；不启用额外去鬼影。
标定在融合前完成，输出使用原生无损 DNG 写入器，不在导出后再转码或重写像素。
完整 Windows 界面和当前后端源码未公开，因此补丁在原包内部加载可审查的适配器；没有替换重建模型或冒充完整上游源码。
手机元数据提供的噪声模型仍是估计值，自动读取不等于重新实测传感器。

## 从源码运行与构建

运行助手只需带 Tk 的 Python 3.12 或更新版本，无第三方运行依赖：

```powershell
py -3.12 -m iqoo_easy
py -3.12 -m pip install -r requirements-test.txt
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
自动测试使用合成元数据、数值样本与假的子进程引擎；另用本机原片和实际 GPU 验证完整处理，不在仓库内保存个人照片。

作者和组件许可见 [NOTICE-EASY.md](NOTICE-EASY.md)。
