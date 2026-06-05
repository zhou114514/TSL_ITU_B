# TSL-ITU-B 程控激光器控制软件

基于 PyQt5 的图形化控制软件，用于通过串口（RS-232/USB 转串口）控制**锐力光电科技有限公司 TSL-ITU-B 系列**程控激光器。支持通过配置文件管理**任意数量**的激光器，并提供 TCP 远程控制接口。

---

## 功能特性

- **多设备可配置**：通过 `config.json` 中的 `lasers` 数组动态管理任意数量的激光器，每台对应一个选项卡
- **串口连接管理**：自动枚举可用 COM 口，一键连接 / 断开
- **状态实时监控**：后台监听线程持续读取串口字节流，自动解析设备应答包及面板主动推送，实时更新界面
- **波长设置**：从 C96 波长表中选择目标波长，支持两个快速预设按钮
- **功率设置**：输入 7 ~ 13 dBm（分辨率 0.01 dBm），带范围校验
- **激光输出控制**：一键开启 / 停止激光输出，按钮颜色实时反映状态
- **操作日志**：带时间戳的滚动日志，记录所有指令发送与状态变化
- **TCP 远程控制**：内置 TCP 服务器，支持通过网络指令远程控制任意一台激光器

---

## 文件结构

```
程控激光器/
├── main.py          # PyQt5 图形界面主程序
├── TSL_ITU_B.py     # 设备通信协议库
├── TCPServer.py     # TCP 远程控制服务器
├── config.json      # 持久化配置（激光器列表、TCP 地址、上次串口）
└── docs/
    └── C96波长表.csv  # C96 通道 ↔ 波长对照表
```

---

## 环境要求

| 依赖       | 版本要求   |
| -------- | ------ |
| Python   | ≥ 3.10 |
| PyQt5    | ≥ 5.15 |
| pyserial | ≥ 3.5  |

安装依赖：

```bash
pip install PyQt5 pyserial
```

---

## 快速开始

```bash
python main.py
```

1. 将激光器通过 USB 转串口线连接到电脑
2. 在对应设备的选项卡中，点击「刷新」选择 COM 口，点击「连接」
3. 连接成功后软件自动读取当前波长、功率和输出状态
4. 在「波长设置」区域选择目标波长并点击「设置波长」
5. 在「功率设置」区域输入目标功率并点击「设置功率」
6. 点击「▶ 开始输出」开启激光输出，再次点击停止

---

## 配置文件

软件启动时自动读取同目录下的 `config.json`，首次运行若不存在则使用内置默认值。

### 激光器列表（`lasers`）

`lasers` 数组中每个元素代表一台激光器，包含多少个元素就显示多少个选项卡。

| 字段            | 必填 | 说明                                                       |
| ------------- | -- | -------------------------------------------------------- |
| `tab_name`    | 是  | 界面选项卡上显示的名称                                              |
| `device_id`   | 是  | 远程控制指令 `"device"` 字段中使用的标识符（匹配时**大小写不敏感**）               |
| `port_cfg_key`| 否  | 记忆上次串口选择的配置键名，省略时自动生成为 `last_port_<device_id>` |

### 示例：三台激光器

```json
{
  "lasers": [
    { "tab_name": "发射端",  "device_id": "Transmitter", "port_cfg_key": "last_port_tx"  },
    { "tab_name": "CCD 端", "device_id": "CCD",          "port_cfg_key": "last_port_ccd" },
    { "tab_name": "备用端",  "device_id": "Spare" }
  ],
  "tcp_host": "127.0.0.1",
  "tcp_port": 10009
}
```

### 其他配置项

| 字段         | 默认值          | 说明             |
| ---------- | ------------ | -------------- |
| `tcp_host` | `"127.0.0.1"` | TCP 服务器监听地址    |
| `tcp_port` | `10009`       | TCP 服务器监听端口    |

---

## TCP 远程控制协议

软件启动后自动在 `tcp_host:tcp_port` 上监听 TCP 连接，消息格式为 **JSON + 换行符** (`\n`)。

### 请求格式

```json
{
  "opcode": "<指令名>",
  "parameter": {
    "device": "<device_id>",
    "<其他参数>": "<值>"
  }
}
```

`"device"` 字段用于指定目标激光器（对应 `config.json` 中各条目的 `device_id`）；省略时默认路由到第一台激光器。

### 响应格式

```json
{
  "IsSuccessful": true,
  "Value": "<返回值或空字符串>",
  "ErrorMessage": "Null"
}
```

### 指令列表

| 指令              | 方向   | 参数                               | 返回值                          |
| --------------- | ---- | -------------------------------- | ---------------------------- |
| `check`         | 查询   | —                                | 软件版本号字符串                     |
| `ConnectDevice` | 查询   | `device`                         | 设备已连接则成功，否则返回错误              |
| `LaserON`       | 控制   | `device`                         | —                            |
| `LaserOFF`      | 控制   | `device`                         | —                            |
| `SetWavelength` | 控制   | `device`、`Wavelength`（nm，字符串）    | —                            |
| `GetWavelength` | 查询   | `device`                         | `{"Wavelength": "<nm字符串>"}` |
| `SetPower`      | 控制   | `device`、`Power`（dBm，数值）         | —                            |
| `GetPower`      | 查询   | `device`                         | `{"Power": <dBm浮点数>}`       |

> `SetWavelength` 支持缩写：`"1540"` 自动映射为 `"1540.56"`，`"1563"` 自动映射为 `"1563.05"`。

### 示例

```json
// 开启 CCD 端激光器
{ "opcode": "LaserON", "parameter": { "device": "CCD" } }

// 设置发射端波长为 1550.12 nm
{ "opcode": "SetWavelength", "parameter": { "device": "Transmitter", "Wavelength": "1550.12" } }

// 查询备用端当前功率
{ "opcode": "GetPower", "parameter": { "device": "Spare" } }
```

---

## 通信协议（串口帧格式）

设备使用自定义 6 字节帧格式，波特率 9600，8N1，无校验位：

```
[HEAD1, HEAD2, ADDR, DATAH, DATAL, SUM]
```

| 字段   | 说明                                                 |
| ---- | -------------------------------------------------- |
| HEAD | 设置：`0x00 0x01`；查询：`0x01 0x00`；应答：`0x01 0x01`       |
| ADDR | 通道：`0x01`；功率：`0x02`；输出开断：`0x03`                    |
| DATA | 16 位大端数据（功率 = 实际值 × 100）                           |
| SUM  | 前 5 字节之和（低 8 位）                                    |

### 直接调用协议库

```python
from TSL_ITU_B import TSL_ITU_B

laser = TSL_ITU_B('COM7')

# 查询当前状态
print(laser.get_channel())   # 通道号 (int)
print(laser.get_power())     # 功率 dBm (float)
print(laser.get_output())    # 输出状态 (bool)

# 设置参数
laser.set_channel(32)        # 设置通道
laser.set_power(10.00)       # 设置功率 dBm
laser.set_output(True)       # 开启输出

laser.disconnect()
```

---

## 许可证

本项目遵循 [Mozilla Public License 2.0](LICENSE)。
