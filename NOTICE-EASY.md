# JSR 主摄助手：组件说明

本 fork 的主摄助手是独立编写的启动界面和 DNG 参数读取工具。
新增的 iqoo_easy/、tests/、launch_iqoo.py、build-windows.ps1 及相关说明采用 iqoo_easy/LICENSE 中的 MIT 许可。
该许可不适用于继承的上游源码、模型、图片或官方二进制文件。

超分辨率算法与模型来自姜尧耕的
[Jiangtherapee Super Resolution](https://github.com/y-g-jiang/Jiangtherapee-Super-Resolution-World-Best)。
主摄助手的下载包不包含或重新授权官方引擎；用户需单独下载并完整解压官方 JSR V18.3.1。
官方引擎及其依赖的声明见官方包中的 THIRD_PARTY_NOTICES.md 和 resources/licenses。

主摄助手运行时使用 Python 与 Tcl/Tk，打包工具为 PyInstaller。
Python 和 Tcl/Tk 的许可随 Windows 下载包提供；PyInstaller 的运行时例外允许分发生成的程序。
没有打包照片、用户配置、实验输出或反编译代码。
