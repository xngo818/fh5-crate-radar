# FH5 Crate Radar

Windows 桌面的实验性 FH5 车包雷达与地图覆盖工具。使用 Python 标准库、Tk 界面、本机 Data Out 遥测和只读进程数据，显示当前读取范围内的候选车包。公开版由现有项目整理而来，保留了代码来源说明，并提供人工合成数据供离线演示。

## 支持范围

| 项目 | 当前范围 |
| --- | --- |
| 系统 | Windows，Python 3.12 x64，需带 Tcl/Tk |
| 游戏接入 | Xbox PC 版 FH5 `3.688.109.0` |
| 实测显示环境 | `2560 × 1440`，DPI `120`（125%） |
| 遥测 | Data Out，`127.0.0.1:53005`，UDP |
| 运行依赖 | Python 标准库，无需安装 Pillow |
| 打包工具 | PyInstaller `6.22.3` |

其他游戏版本、Steam 版、分辨率或缩放组合未完成适配验证。地图覆盖需要游戏版本、当前状态和窗口/地图校验通过；校验不通过时保持关闭（fail closed）。自由漫游等没有有效车包状态的场景也可能不显示地图覆盖。详细限制见 [SUPPORTED_BUILD.md](docs/SUPPORTED_BUILD.md)。

候选数量只表示本次读取范围，不能据此判断全地图车包总数。没有固定扫描耗时或识别完整率保证。公开版车型名称表只保留车包映射涉及的 45 个名称；其他本车名称可能为空，车包识别不受这一裁剪影响。

## 从源码启动

1. 安装 Python 3.12 x64，保留安装器的 Tcl/Tk 组件，并确保可以运行 `python`。可用 `python -m tkinter` 检查 Tk 窗口。
2. 将仓库完整下载或解压到一个目录，在该目录双击 `Start_FH5.cmd`；也可在终端运行：

   ```powershell
   python fh5_app.py
   ```

3. 接入支持的游戏版本时，在游戏的 Data Out 设置中启用输出，目标 IP 设为 `127.0.0.1`，目标端口设为 `53005`。以工具界面的遥测、读取和校验状态为准。

`requirements.txt` 记录了运行时没有第三方 pip 依赖。首次查看效果时，可先使用下面的离线演示。

## 无游戏离线演示

`examples/demo.json` 是人工构造的演示数据，所有坐标、地址及会话标记都是虚构值。回放模式不连接游戏进程，也不启动 UDP 监听。

双击 `Start_Demo.cmd` 查看雷达和控制面板，或运行：

```powershell
python fh5_app.py --replay examples/demo.json
```

双击 `Start_Map_Demo.cmd` 查看地图投影演示，或运行：

```powershell
python fh5_app.py --map-preview --replay examples/demo.json
```

地图演示使用空白背景及自绘圆点、数字；仓库不附带游戏地图底图和游戏等级图标。

无窗口自检：

```powershell
python fh5_app.py --replay examples/demo.json --headless --seconds 1
python fh5_app.py --map-preview --replay examples/demo.json --headless --seconds 1
```

这些命令检查离线数据与渲染流程，不证明当前游戏版本的实时接入成功。

## 操作

| 操作 | 默认按键/设置 |
| --- | --- |
| 切换雷达显示 | 手柄 `X` 或键盘 `F9` |
| 切换地图覆盖 | 手柄 `Y`，也可用面板按钮 |
| 退出 | 键盘 `End` 或面板“退出” |
| 最低车包等级 | 默认 `4`，面板可调 `4–10` |
| 雷达直径、透明度 | 在控制面板调整 |

手柄切换键可在面板中更改。雷达以车头朝上，并随车辆朝向旋转；地图覆盖是否显示仍受校验状态限制。

## 测试与 Windows 打包

在仓库根目录运行标准库测试：

```powershell
python -m unittest discover -s tests -v
```

构建前安装打包依赖，再运行构建脚本：

```powershell
python -m pip install -r requirements-dev.txt
powershell -NoProfile -ExecutionPolicy Bypass -File .\Build_Windows.ps1
```

构建输出为 `dist/FH5CrateRadar.exe`。请使用 Python 3.12 x64 在 Windows 上构建。离线测试、EXE 构建成功和真实游戏接入是不同的验证结果；发布时应分别说明。

## 用 GitHub 网页上传

1. 在 GitHub 创建空仓库，例如 `fh5-crate-radar`；已有的 README 和 LICENSE 随本目录一起提供，无需再生成一份。
2. 进入仓库，选择 **Add file → Upload files**（空仓库可选 **uploading an existing file**），上传本目录内的源码、`data`、`docs`、`examples`、`tests` 和根目录文件。让 `fh5_app.py` 与 `README.md` 位于仓库根目录，记得包含 `.gitignore`。
3. 检查上传列表，填写提交说明并提交；完成后在仓库首页确认 README、源码和演示文件均可打开。[GitHub 上传说明](https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository)

网页上传前需自行核对列表，不能依赖 `.gitignore` 替网页筛选文件。只上传这份公开副本；不上传旧 EXE、真实采样、内存转储、会话状态、地址/扫描范围配置、授权密钥、旧 FH6 模块或第三方研究工具。构建产生的 `build`、`dist`、虚拟环境和缓存也不属于源码上传内容。

## 来源、许可与使用规则

本地发布副本默认采用 [MIT License](LICENSE)，版权标记为 `Copyright (c) 2026 Project contributors`。MIT 允许使用、修改、再分发和商用，要求保留版权和许可声明。[MIT 说明](https://choosealicense.com/licenses/mit/)。`pool.py` 保留了从所提供 CPython 3.13 字节码人工重建的说明，不能将本次整理描述为所有模块均从零编写。运行组件、构建工具和游戏资产的范围见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

本项目与 Microsoft、Xbox、Playground Games 或 Turn 10 没有官方关联。公开源码或采用 MIT 许可证不代表获得游戏方许可。Xbox 社区标准禁止利用专用软件取得不公平优势等行为，违规可能导致账号或设备限制；使用前应自行了解并遵守适用规则。参见 [Xbox 社区标准](https://www.xbox.com/en-US/legal/community-standards)。

游戏名称、商标、原游戏等级图标和地图资产不随本项目的 MIT 许可证授权；原游戏 PNG 和地图底图未包含在公开副本中。
