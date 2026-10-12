# IWR6843AOP 点云可视化与手势采集

本目录是可独立提交的 Python 项目：网页版实时点云、桌面查看器、串口诊断、手势片段录制、NPZ 导出与离线回放。不包含其它学习项目、模型、固件或采集数据。

## 环境与已知限制

- 实测环境：Windows、Python 3.12、IWR6843AOP + CP2105 双串口。
- Enhanced 为 CLI（115200），Standard 为数据口（921600）；COM 编号因机器而异。
- 默认 **15 fps**。当前板子的 20 fps 配置曾停止响应，**页面/API切换已禁用**；历史配置仅作为研究参考，勿当作稳定采集方案。
- 当前固件不支持仅发 `sensorStop/frameCfg/sensorStart` 修改帧率后启动；恢复配置必须完整下发。
- 只监听 `127.0.0.1:8080`，无认证，不适合公网部署。每次只能由一个程序占用串口。
- 当前解析器针对 TI demo 的 float32 XY Z/velocity 点云与 side-info TLV，并不是任意固件通用解析器。
- NPZ 使用 object arrays/pickle，仅加载自己生成、来源可信的文件。不要加载来历不明的数据包。
- 15 fps 已通过短时实板测试，不代表已完成小时级连续稳定性验证。

## 安装与运行

在本目录打开 PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe web_viewer.py
```

浏览器访问 http://127.0.0.1:8080 。Windows 也可以双击 `启动网页版.bat`；它优先使用项目内 `.venv`，其次上级 `.venv`，最后系统 Python。**用 Ctrl+C 正常停止，不要直接强杀进程**，正常停止会尝试先停雷达再关接收。

```powershell
python web_viewer.py --cli COM7 --data COM8
python web_viewer.py --http-port 8081
python web_viewer.py --no-config  # 只接收已启动雷达；配置来源标记unknown
python realtime_viewer.py       # 桌面版，与网页版互斥
python check_ports.py           # 先关闭查看器，仅诊断，不自动启动雷达
python check_ports.py --configure --seconds 5  # 完整配置测试，结束时停止雷达
```

若无有效帧：先看具体错误。`sensorStart`拒绝通常是配置未完整下发；CLI也完全无回应时才需要断开全部电源重插。仅刷新页面不能复位固件。

## 采集与导出

1. 确认页面“运行中”、帧号更新，保持15 fps。
2. 选择标签、被试、0.5–10秒时长，点击片段采集。标签/被试支持文字、数字、下划线、短横线，不允许路径。
3. 完成片段存入 `recordings/clips/<标签>/`。录制中使用 `.jsonl.part`，只有完成后才变为 `.jsonl`；中断的part文件不计入导出。
4. 导出只读取完成片段。每个新片段记录自己的配置来源；旧片段缺少配置时标记unknown，不用当前配置冒充历史配置。
5. 撤销删除最近完成的片段，请确认不再需要；导出时不允许撤销。

```powershell
python load_dataset.py                    # 最新NPZ质检
python load_dataset.py --file <可信文件>
python load_dataset.py --replay 0         # 离线回放
```

每个点为 `[x,y,z,doppler,snr,noise]`，单位为米、米/秒、dB。ROI只影响采集ROI点，不改变原始点；记录同时含`pts`与`pts_raw`。导出`meta.keys`列出有效片段key，`__raw`、`__frames`、`__times`为辅助字段。空帧为`(0,6)`。

## 目录

```text
web/index.html                  前端，无需Node构建
web_viewer.py                   HTTP/WebSocket、录制、导出、串口工作线程
realtime_viewer.py              共用配置/端口/解析器，桌面显示入口
check_ports.py                  独立串口诊断
load_dataset.py                 可信NPZ质检与回放
selftest.py                     合成帧与性能/可选GUI测试
test_connection.py              离线连接回归测试
test_acquisition.py             采集/导出离线回归测试
.github/workflows/tests.yml      Windows/Python3.12自动测试
verify_live_connection.py        已运行后台的实板接收测试
verify_browser.js               可选Tabbit浏览器验证脚本
*.cfg                           历史雷达配置参考
recordings/                     本地私有数据，不提交
```

## 测试

```powershell
python -m unittest discover -p "test_*.py" -v
python -c "import selftest; selftest.test_correctness(); selftest.test_throughput()"
# 需要已启动网页后台与实板：
python verify_live_connection.py
```

`verify_browser.js` 是开发用Tabbit验证程序，不是平台启动依赖。GUI测试需要真实显示器。不要同时跑串口诊断与正在使用串口的查看器。
