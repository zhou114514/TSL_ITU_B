"""
TSL-ITU-B 程控激光器控制软件  (PyQt5) — 多设备可配置版本

通过 config.json 中的 "lasers" 数组动态管理任意数量的激光器，
每个元素对应一台激光器，至少包含两个字段：
    tab_name   — 界面选项卡上显示的名称
    device_id  — 远程控制指令中 "device" 字段对应的标识符
可选字段：
    port_cfg_key — 存储上次串口选择的键名（省略时自动生成）

远程控制协议：在所有指令中携带 "device" 字段以指定目标设备；
若未携带 "device" 字段，默认路由到第一台激光器。

状态监控策略：
  - 连接后启动后台监听线程，持续读取串口字节流
  - 监听线程将所有合法的 6 字节应答包（含面板主动推送）解析后通过信号更新界面
  - 本地发送指令时仅记录"已发送"日志；界面数值在监听线程收到应答后更新
"""

import csv
import json
import os
import sys
import time
import threading
from datetime import datetime

from TCPServer import TCPServer
import serial.tools.list_ports
from PyQt5.QtCore import Qt, pyqtSignal, QObject
from PyQt5.QtGui import QFont, QDoubleValidator, QIntValidator
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QLabel, QComboBox, QPushButton, QLineEdit,
    QTextEdit, QMessageBox, QSizePolicy, QTabWidget,
    QDialog, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
)

from TSL_ITU_B import TSL_ITU_B, READ_HEAD, CHANNEL_ADDR, POWER_ADDR, OUTPUT_ADDR

if getattr(sys, "frozen", False):
    SYS_PATH = os.path.dirname(sys.executable)
else:
    SYS_PATH = os.path.dirname(os.path.abspath(__file__))

# ── 配置文件 ─────────────────────────────────────────────────────────────── #
CONFIG_PATH = os.path.join(SYS_PATH, "config.json")

# 默认激光器列表，兼容旧版双设备配置
_DEFAULT_LASERS = [
    {"tab_name": "发射端", "device_id": "Transmitter", "port_cfg_key": "last_port_tx"},
    {"tab_name": "CCD 端",  "device_id": "CCD",          "port_cfg_key": "last_port_ccd"},
]

DEFAULT_CONFIG: dict = {
    "lasers": _DEFAULT_LASERS,
    "tcp_host": "127.0.0.1",
    "tcp_port": 10009,
}
VERSION = "1.1.0"


def load_config() -> dict:
    """加载配置文件，若文件不存在或解析失败则返回默认值"""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            cfg = dict(DEFAULT_CONFIG)
            cfg.update(data)
            # 兼容旧版 last_port 字段（旧版只有 last_port，无 last_port_tx）
            if "last_port" in data and not cfg.get("last_port_tx"):
                cfg["last_port_tx"] = data["last_port"]
            return cfg
        except Exception:
            pass
    else:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    """将配置写入文件"""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存配置失败: {e}")


def load_wavelength_table():
    """从 docs 目录读取波长通道对应表，返回 (wl_list, wl_table)"""
    docs_dir = os.path.join(SYS_PATH, "docs")
    csv_path = None
    for f in os.listdir(docs_dir):
        if f.endswith(".csv"):
            csv_path = os.path.join(docs_dir, f)
            break
    if csv_path is None:
        raise FileNotFoundError("未找到 docs 目录下的 CSV 文件")

    table: dict[str, int] = {}
    wl_list: list[str] = []
    with open(csv_path, encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            wl = row["波长WL/nm"].strip()
            chn_num = int(row["CHN_NUM"].strip())
            if wl:
                table[wl] = chn_num
                wl_list.append(wl)
    return wl_list, table


def build_chn_to_wl(wl_table: dict[str, int]) -> dict[int, str]:
    return {chn: wl for wl, chn in wl_table.items()}


# ── 线程信号桥 ──────────────────────────────────────────────────────────── #
class _Signals(QObject):
    log            = pyqtSignal(str)
    wl_updated     = pyqtSignal(str)   # 波长显示文本
    pw_updated     = pyqtSignal(str)   # 功率显示文本
    output_updated = pyqtSignal(bool)  # 输出开关状态


# ── 限高下拉框 ──────────────────────────────────────────────────────────── #
class ScrollableComboBox(QComboBox):
    """弹出列表限制最大像素高度，超出部分滚动显示"""

    def __init__(self, max_popup_height=300, parent=None):
        super().__init__(parent)
        self._max_popup_height = max_popup_height

    def showPopup(self):
        super().showPopup()
        popup = self.view().parent()
        geo = popup.geometry()
        if geo.height() <= self._max_popup_height:
            return

        screen_rect = QApplication.desktop().availableGeometry(self)
        below = self.mapToGlobal(self.rect().bottomLeft())
        above = self.mapToGlobal(self.rect().topLeft())

        geo.setHeight(self._max_popup_height)
        if below.y() + self._max_popup_height <= screen_rect.bottom():
            geo.moveTopLeft(below)
        else:
            geo.moveBottomLeft(above)
        popup.setGeometry(geo)


# ── 单设备控制面板 ──────────────────────────────────────────────────────── #
class LaserPanel(QWidget):
    """封装单台激光器的串口连接、状态显示、波长/功率/输出控制及操作日志"""

    QUICK_WL = ["1540.56", "1550.12", "1563.05"]

    def __init__(
        self,
        label: str,
        wl_list: list,
        wl_table: dict,
        chn_to_wl: dict,
        port_cfg_key: str,
        cfg: dict,
        parent=None,
    ):
        super().__init__(parent)
        self.label = label
        self.wl_list = wl_list
        self.wl_table = wl_table
        self.chn_to_wl = chn_to_wl
        self.port_cfg_key = port_cfg_key
        self.cfg = cfg

        self.device: TSL_ITU_B | None = None
        self.output_on = False
        self._stop_listener = threading.Event()
        self._listener_thread_obj: threading.Thread | None = None

        self._query_lock = threading.Lock()
        self._pending_addr: int | None = None
        self._pending_result: bytes | None = None
        self._pending_event = threading.Event()

        self._sig = _Signals()
        self._sig.log.connect(self._append_log)
        self._sig.output_updated.connect(self._on_output_updated)

        self._build_ui()

        # 控件构建完成后才能绑定到真实槽
        self._sig.wl_updated.connect(self.disp_wl.setText)
        self._sig.pw_updated.connect(self.disp_pw.setText)

        self._set_controls_enabled(False)

        # 恢复上次选择的串口
        last_port = self.cfg.get(self.port_cfg_key, "")
        if last_port:
            idx = self.port_combo.findText(last_port)
            if idx >= 0:
                self.port_combo.setCurrentIndex(idx)

    # ------------------------------------------------------------------ #
    #  UI 构建                                                              #
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(14, 12, 14, 12)

        # ── 串口连接区 ────────────────────────────────────────────────── #
        conn_group = QGroupBox("串口连接")
        conn_layout = QHBoxLayout(conn_group)
        conn_layout.setSpacing(8)

        conn_layout.addWidget(QLabel("串口:"))
        self.port_combo = QComboBox()
        self.port_combo.setFixedWidth(90)
        conn_layout.addWidget(self.port_combo)

        btn_refresh = QPushButton("刷新")
        btn_refresh.setFixedWidth(54)
        btn_refresh.clicked.connect(self._refresh_ports)
        conn_layout.addWidget(btn_refresh)

        self.btn_connect = QPushButton("连接")
        self.btn_connect.setFixedWidth(70)
        self.btn_connect.clicked.connect(self._toggle_connection)
        conn_layout.addWidget(self.btn_connect)

        conn_layout.addStretch()
        conn_layout.addWidget(QLabel("状态:"))
        self.status_label = QLabel("未连接")
        self.status_label.setFixedWidth(60)
        self.status_label.setStyleSheet("color:red; font-weight:bold;")
        conn_layout.addWidget(self.status_label)

        self._refresh_ports()
        root.addWidget(conn_group)

        # ── 当前状态显示区 ────────────────────────────────────────────── #
        info_group = QGroupBox("当前状态")
        info_layout = QHBoxLayout(info_group)
        info_layout.setSpacing(16)

        info_layout.addWidget(QLabel("当前波长:"))
        self.disp_wl = QLineEdit("—")
        self.disp_wl.setReadOnly(True)
        self.disp_wl.setFixedWidth(110)
        self.disp_wl.setAlignment(Qt.AlignCenter)
        self.disp_wl.setStyleSheet(
            "QLineEdit { background:#f0f4f8; color:#1a3a5c; font-weight:bold;"
            " border:1px solid #b0bec5; border-radius:3px; padding:2px; }"
        )
        info_layout.addWidget(self.disp_wl)
        info_layout.addWidget(QLabel("nm"))

        info_layout.addSpacing(20)

        info_layout.addWidget(QLabel("当前功率:"))
        self.disp_pw = QLineEdit("—")
        self.disp_pw.setReadOnly(True)
        self.disp_pw.setFixedWidth(90)
        self.disp_pw.setAlignment(Qt.AlignCenter)
        self.disp_pw.setStyleSheet(
            "QLineEdit { background:#f0f4f8; color:#1a3a5c; font-weight:bold;"
            " border:1px solid #b0bec5; border-radius:3px; padding:2px; }"
        )
        info_layout.addWidget(self.disp_pw)
        info_layout.addWidget(QLabel("dBm"))
        info_layout.addStretch()

        root.addWidget(info_group)

        # ── 波长设置区 ────────────────────────────────────────────────── #
        wl_group = QGroupBox("波长设置")
        wl_layout = QVBoxLayout(wl_group)
        wl_layout.setSpacing(8)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("波长 (nm):"))
        self.wl_combo = ScrollableComboBox(max_popup_height=300)
        self.wl_combo.addItems(self.wl_list)
        self.wl_combo.setFixedWidth(110)
        row1.addWidget(self.wl_combo)

        self.btn_set_wl = QPushButton("设置波长")
        self.btn_set_wl.setFixedWidth(90)
        self.btn_set_wl.clicked.connect(self._set_wavelength)
        row1.addWidget(self.btn_set_wl)
        row1.addStretch()
        wl_layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("快速设置:"))
        self.quick_btns: list[QPushButton] = []
        for wl in self.QUICK_WL:
            btn = QPushButton(f"{wl} nm")
            btn.setFixedWidth(110)
            btn.clicked.connect(lambda checked, w=wl: self._quick_set_wavelength(w))
            row2.addWidget(btn)
            self.quick_btns.append(btn)
        row2.addStretch()
        wl_layout.addLayout(row2)

        root.addWidget(wl_group)

        # ── 功率设置区 ────────────────────────────────────────────────── #
        pw_group = QGroupBox("功率设置")
        pw_layout = QHBoxLayout(pw_group)
        pw_layout.setSpacing(8)

        pw_layout.addWidget(QLabel("功率 (dBm):"))
        self.power_edit = QLineEdit("10.00")
        self.power_edit.setFixedWidth(90)
        validator = QDoubleValidator(7.0, 13.0, 2, self.power_edit)
        validator.setNotation(QDoubleValidator.StandardNotation)
        self.power_edit.setValidator(validator)
        pw_layout.addWidget(self.power_edit)

        hint = QLabel("(范围: 7 ~ 13 dBm)")
        hint.setStyleSheet("color:gray;")
        pw_layout.addWidget(hint)

        self.btn_set_pw = QPushButton("设置功率")
        self.btn_set_pw.setFixedWidth(90)
        self.btn_set_pw.clicked.connect(self._set_power)
        pw_layout.addWidget(self.btn_set_pw)
        pw_layout.addStretch()

        root.addWidget(pw_group)

        # ── 输出控制区 ────────────────────────────────────────────────── #
        out_group = QGroupBox("输出控制")
        out_layout = QVBoxLayout(out_group)

        self.btn_output = QPushButton("▶   开始输出")
        self.btn_output.setFixedHeight(52)
        self.btn_output.setFont(QFont("微软雅黑", 12, QFont.Bold))
        self.btn_output.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.btn_output.clicked.connect(self._toggle_output)
        self._apply_output_style(False)
        out_layout.addWidget(self.btn_output)

        root.addWidget(out_group)

        # ── 操作日志区 ────────────────────────────────────────────────── #
        log_group = QGroupBox("操作日志")
        log_layout = QVBoxLayout(log_group)

        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setFont(QFont("Consolas", 9))
        self.log_edit.setFixedHeight(150)
        log_layout.addWidget(self.log_edit)

        root.addWidget(log_group)

    # ------------------------------------------------------------------ #
    #  串口操作                                                             #
    # ------------------------------------------------------------------ #
    def _refresh_ports(self):
        current = self.port_combo.currentText()
        self.port_combo.clear()
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_combo.addItems(ports)
        if current in ports:
            self.port_combo.setCurrentText(current)

    def _toggle_connection(self):
        if self.device is None:
            self._connect()
        else:
            self._disconnect()

    def _connect(self):
        port = self.port_combo.currentText()
        if not port:
            QMessageBox.warning(self, "提示", "请先选择串口")
            return
        try:
            self.device = TSL_ITU_B(port)
            self.cfg[self.port_cfg_key] = port
            save_config(self.cfg)
            self.status_label.setText("已连接")
            self.status_label.setStyleSheet("color:green; font-weight:bold;")
            self.btn_connect.setText("断开")
            self._set_controls_enabled(True)
            self._log(f"[{self.label}] 已连接到 {port}，正在读取初始状态…")
            self._stop_listener.clear()
            t = threading.Thread(target=self._init_then_listen, daemon=True)
            self._listener_thread_obj = t
            t.start()
        except Exception as e:
            self.device = None
            QMessageBox.critical(self, "连接失败", str(e))
            self._log(f"[{self.label}] 连接失败: {e}")

    def _disconnect(self):
        self._stop_listener.set()
        # 先等待监听线程退出，再关闭串口
        # 避免 Windows 下 CloseHandle 与 ReadFile 同时操作同一句柄导致死锁
        if self._listener_thread_obj is not None:
            self._listener_thread_obj.join(timeout=2.0)
            self._listener_thread_obj = None
        if self.device:
            try:
                if self.output_on:
                    self.device.cmd_output(False)
                self.device.disconnect()
            except Exception:
                pass
            self.device = None
        self.output_on = False
        self.status_label.setText("未连接")
        self.status_label.setStyleSheet("color:red; font-weight:bold;")
        self.btn_connect.setText("连接")
        self._set_controls_enabled(False)
        self._apply_output_style(False)
        self.disp_wl.setText("—")
        self.disp_pw.setText("—")
        self._log(f"[{self.label}] 已断开连接")

    # ------------------------------------------------------------------ #
    #  初始查询 + 监听线程                                                   #
    # ------------------------------------------------------------------ #
    def _init_then_listen(self):
        if self.device is None:
            return

        try:
            chn = self.device.get_channel()
            if chn is not None:
                wl = self.chn_to_wl.get(chn, f"通道 {chn}")
                self._sig.wl_updated.emit(wl)
                self._sig.log.emit(self._ts(f"初始波长: {wl} nm  (通道 {chn})"))
            else:
                self._sig.log.emit(self._ts("初始波长查询无应答"))
        except Exception as e:
            self._sig.log.emit(self._ts(f"初始波长查询异常: {e}"))
        time.sleep(0.1)

        try:
            pw = self.device.get_power()
            if pw is not None:
                self._sig.pw_updated.emit(f"{pw:.2f}")
                self._sig.log.emit(self._ts(f"初始功率: {pw:.2f} dBm"))
            else:
                self._sig.log.emit(self._ts("初始功率查询无应答"))
        except Exception as e:
            self._sig.log.emit(self._ts(f"初始功率查询异常: {e}"))
        time.sleep(0.1)

        try:
            out = self.device.get_output()
            if out is not None:
                self._sig.output_updated.emit(out)
                self._sig.log.emit(
                    self._ts(f"初始输出状态: {'开启' if out else '关闭'}")
                )
            else:
                self._sig.log.emit(self._ts("初始输出状态查询无应答"))
        except Exception as e:
            self._sig.log.emit(self._ts(f"初始输出状态查询异常: {e}"))
        time.sleep(0.1)

        self._sig.log.emit(self._ts("初始状态读取完成，进入串口监听"))
        self._listener_thread()

    def _listener_thread(self):
        ser = self.device.ser
        buf = bytearray()

        while not self._stop_listener.is_set():
            try:
                byte = ser.read(1)
            except Exception:
                break

            if not byte:
                continue

            buf += byte

            while len(buf) >= 6:
                if buf[0] == 0x01 and buf[1] == 0x01:
                    packet = bytes(buf[:6])
                    if self.device and self.device.check_data(packet):
                        self._parse_packet(packet)
                        buf = buf[6:]
                    else:
                        buf = buf[1:]
                else:
                    buf = buf[1:]

    def _parse_packet(self, packet: bytes):
        addr  = packet[2]
        value = int.from_bytes(packet[3:5], "big")

        if self._pending_addr is not None and addr == self._pending_addr:
            self._pending_result = packet
            self._pending_event.set()

        if addr == CHANNEL_ADDR:
            wl = self.chn_to_wl.get(value, f"通道 {value}")
            self._sig.wl_updated.emit(wl)
            self._sig.log.emit(self._ts(f"波长更新: {wl} nm  (通道 {value})"))

        elif addr == POWER_ADDR:
            power = value / 100
            self._sig.pw_updated.emit(f"{power:.2f}")
            self._sig.log.emit(self._ts(f"功率更新: {power:.2f} dBm"))

        elif addr == OUTPUT_ADDR:
            on = (value == 0x0101)
            self._sig.output_updated.emit(on)
            self._sig.log.emit(self._ts("激光输出已开启" if on else "激光输出已关闭"))

    # ------------------------------------------------------------------ #
    #  线程安全查询（经由监听线程中转应答）                                        #
    # ------------------------------------------------------------------ #
    def query_device(self, addr: int, timeout: float = 2.0) -> bytes | None:
        """发送查询指令并等待监听线程捕获应答包，避免与监听线程竞争串口读取。"""
        if self.device is None:
            return None
        with self._query_lock:
            self._pending_event.clear()
            self._pending_result = None
            self._pending_addr = addr

            data = [*READ_HEAD, addr, 0, 0]
            data.append(self.device.sum_data(data))
            self.device.send_data(data)

            got = self._pending_event.wait(timeout=timeout)
            result = self._pending_result

            self._pending_addr = None
            self._pending_result = None
        return result if got else None

    def get_channel_via_listener(self) -> int | None:
        packet = self.query_device(CHANNEL_ADDR)
        if packet and self.device and self.device.check_data(packet):
            return int.from_bytes(packet[3:5], "big")
        return None

    def get_power_via_listener(self) -> float | None:
        packet = self.query_device(POWER_ADDR)
        if packet and self.device and self.device.check_data(packet):
            return int.from_bytes(packet[3:5], "big") / 100
        return None

    def get_output_via_listener(self) -> bool | None:
        packet = self.query_device(OUTPUT_ADDR)
        if packet and self.device and self.device.check_data(packet):
            return packet[3:5] == b"\x01\x01"
        return None

    def cmd_and_wait(self, cmd_fn, addr: int, timeout: float = 3.0) -> bool:
        """发送写指令并等待监听线程捕获硬件应答，线程安全。

        利用已有的 _query_lock 保证同一设备的串口操作串行化：
        持锁期间监听线程仍在读串口，一旦收到与 addr 匹配的应答包
        即通过 _pending_event 通知本方法返回。
        """
        if self.device is None:
            return False
        with self._query_lock:
            self._pending_event.clear()
            self._pending_result = None
            self._pending_addr = addr
            try:
                cmd_fn()
            except Exception:
                self._pending_addr = None
                self._pending_result = None
                return False
            got = self._pending_event.wait(timeout=timeout)
            self._pending_addr = None
            self._pending_result = None
        return got

    # ------------------------------------------------------------------ #
    #  波长控制                                                             #
    # ------------------------------------------------------------------ #
    def _set_wavelength(self):
        wl = self.wl_combo.currentText()
        if not wl:
            QMessageBox.warning(self, "提示", "请选择波长")
            return
        chn = self.wl_table.get(wl)
        if chn is None:
            QMessageBox.critical(self, "错误", f"未找到波长 {wl} 对应的通道号")
            return
        self._run_in_thread(self._do_send_channel, wl, chn)

    def _quick_set_wavelength(self, wl: str):
        chn = self.wl_table.get(wl)
        if chn is None:
            QMessageBox.critical(self, "错误", f"未找到波长 {wl} 对应的通道号")
            return
        idx = self.wl_combo.findText(wl)
        if idx >= 0:
            self.wl_combo.setCurrentIndex(idx)
        self._run_in_thread(self._do_send_channel, wl, chn)

    def _do_send_channel(self, wl: str, chn: int):
        try:
            self.device.cmd_channel(chn)
            self._sig.log.emit(self._ts(f"已发送波长设置指令: {wl} nm  (通道 {chn})"))
        except Exception as e:
            self._sig.log.emit(self._ts(f"波长设置异常: {e}"))

    # ------------------------------------------------------------------ #
    #  功率控制                                                             #
    # ------------------------------------------------------------------ #
    def _set_power(self):
        text = self.power_edit.text()
        try:
            power = float(text)
        except ValueError:
            QMessageBox.warning(self, "提示", "请输入有效的功率值")
            return
        if power < 7 or power > 13:
            QMessageBox.warning(self, "提示", "功率范围为 7 ~ 13 dBm")
            return
        self._run_in_thread(self._do_send_power, power)

    def _do_send_power(self, power: float):
        try:
            self.device.cmd_power(power)
            self._sig.log.emit(self._ts(f"已发送功率设置指令: {power:.2f} dBm"))
        except Exception as e:
            self._sig.log.emit(self._ts(f"功率设置异常: {e}"))

    # ------------------------------------------------------------------ #
    #  输出控制                                                             #
    # ------------------------------------------------------------------ #
    def _toggle_output(self):
        self._run_in_thread(self._do_send_output)

    def _do_send_output(self):
        try:
            target = not self.output_on
            self.device.cmd_output(target)
            self._sig.log.emit(
                self._ts(f"已发送{'开启' if target else '关闭'}输出指令")
            )
        except Exception as e:
            self._sig.log.emit(self._ts(f"输出控制异常: {e}"))

    def _on_output_updated(self, on: bool):
        self.output_on = on
        self._apply_output_style(on)

    def _apply_output_style(self, on: bool):
        if on:
            self.btn_output.setText("■   停止输出")
            self.btn_output.setStyleSheet(
                "QPushButton { background:#e74c3c; color:white; border:none; border-radius:4px; }"
                "QPushButton:hover { background:#c0392b; }"
                "QPushButton:pressed { background:#a93226; }"
                "QPushButton:disabled { background:#aaa; color:#ddd; }"
            )
        else:
            self.btn_output.setText("▶   开始输出")
            self.btn_output.setStyleSheet(
                "QPushButton { background:#2ecc71; color:white; border:none; border-radius:4px; }"
                "QPushButton:hover { background:#27ae60; }"
                "QPushButton:pressed { background:#1e8449; }"
                "QPushButton:disabled { background:#aaa; color:#ddd; }"
            )

    # ------------------------------------------------------------------ #
    #  辅助方法                                                             #
    # ------------------------------------------------------------------ #
    def _set_controls_enabled(self, enabled: bool):
        for w in (
            self.btn_set_wl, self.btn_set_pw, self.btn_output,
            self.power_edit, self.wl_combo, *self.quick_btns,
        ):
            w.setEnabled(enabled)

    def _run_in_thread(self, func, *args):
        t = threading.Thread(target=func, args=args, daemon=True)
        t.start()

    def _ts(self, msg: str) -> str:
        return f"[{datetime.now().strftime('%H:%M:%S')}]  {msg}"

    def _log(self, msg: str):
        self._sig.log.emit(self._ts(msg))

    def _append_log(self, line: str):
        self.log_edit.append(line)
        self.log_edit.verticalScrollBar().setValue(
            self.log_edit.verticalScrollBar().maximum()
        )


# ── 配置对话框 ───────────────────────────────────────────────────────────── #
class ConfigDialog(QDialog):
    """激光器配置对话框"""

    PRESET_DEFAULT = [
        {"tab_name": "发射端", "device_id": "Transmitter", "port_cfg_key": "last_port_tx"},
        {"tab_name": "CCD 端",  "device_id": "CCD",          "port_cfg_key": "last_port_ccd"},
    ]
    PRESET_REAR_OPTICAL = [
        {"tab_name": "发射A", "device_id": "A"},
        {"tab_name": "发射B", "device_id": "B"},
        {"tab_name": "基准镜", "device_id": "JZJ"},
        {"tab_name": "接收",   "device_id": "CCD"},
    ]

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("软件配置")
        self.setFixedWidth(500)
        self.setModal(True)
        self._build_ui()
        self._load_from_cfg()

    # ------------------------------------------------------------------ #
    #  UI 构建                                                              #
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(16, 16, 16, 16)

        # ── 标题 ──────────────────────────────────────────────────────── #
        title = QLabel("软件配置")
        title.setAlignment(Qt.AlignCenter)
        title.setFixedHeight(40)
        title.setFont(QFont("微软雅黑", 13, QFont.Bold))
        title.setStyleSheet(
            "background:#1a3a5c; color:white; border-radius:3px;"
        )
        root.addWidget(title)

        # ── 激光器配置 ────────────────────────────────────────────────── #
        laser_group = QGroupBox("激光器配置")
        laser_layout = QVBoxLayout(laser_group)
        laser_layout.setSpacing(8)

        # 预设按钮行
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("快速预设:"))

        btn_default = QPushButton("默认配置")
        btn_default.setFixedWidth(100)
        btn_default.setToolTip("发射端 (Transmitter) + CCD 端 (CCD)")
        btn_default.clicked.connect(lambda: self._load_preset(self.PRESET_DEFAULT))
        preset_row.addWidget(btn_default)

        btn_rear = QPushButton("后光路测试")
        btn_rear.setFixedWidth(100)
        btn_rear.setToolTip("发射A / 发射B / 基准镜 / 接收，共四台激光器")
        btn_rear.clicked.connect(lambda: self._load_preset(self.PRESET_REAR_OPTICAL))
        preset_row.addWidget(btn_rear)

        preset_row.addStretch()
        laser_layout.addLayout(preset_row)

        # 激光器列表表格
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["选项卡名称", "远程控制 ID", ""])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
        self.table.setColumnWidth(2, 52)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked)
        self.table.setStyleSheet(
            "QTableWidget { border:1px solid #c0c0c0; gridline-color:#e0e0e0; }"
            "QHeaderView::section { background:#f0f4f8; font-weight:bold;"
            "  border:none; border-bottom:1px solid #c0c0c0; padding:4px; font-family:微软雅黑; }"
        )
        self.table.setFixedHeight(160)
        laser_layout.addWidget(self.table)

        # 添加行按钮
        btn_add = QPushButton("＋  添加激光器")
        btn_add.setFixedHeight(28)
        btn_add.setStyleSheet(
            "QPushButton { border:1px dashed #aaa; border-radius:3px; color:#555; font-family:微软雅黑; }"
            "QPushButton:hover { background:#f0f4f8; border-color:#1a3a5c; color:#1a3a5c; }"
        )
        btn_add.clicked.connect(lambda: self._add_row())
        laser_layout.addWidget(btn_add)

        root.addWidget(laser_group)

        # ── TCP 配置 ──────────────────────────────────────────────────── #
        tcp_group = QGroupBox("TCP 远程控制")
        tcp_layout = QHBoxLayout(tcp_group)
        tcp_layout.setSpacing(8)

        tcp_layout.addWidget(QLabel("监听地址:"))
        self.host_edit = QLineEdit()
        self.host_edit.setFixedWidth(140)
        tcp_layout.addWidget(self.host_edit)

        tcp_layout.addSpacing(16)
        tcp_layout.addWidget(QLabel("端口:"))
        self.port_edit = QLineEdit()
        self.port_edit.setFixedWidth(70)
        self.port_edit.setValidator(QIntValidator(1, 65535, self))
        tcp_layout.addWidget(self.port_edit)
        tcp_layout.addStretch()

        root.addWidget(tcp_group)

        # ── 底部按钮 ──────────────────────────────────────────────────── #
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        btn_cancel = QPushButton("取消")
        btn_cancel.setFixedSize(88, 32)
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)

        btn_save = QPushButton("保存")
        btn_save.setFixedSize(88, 32)
        btn_save.setStyleSheet(
            "QPushButton { background:#1a3a5c; color:white; border:none; border-radius:4px; font-family:微软雅黑; }"
            "QPushButton:hover { background:#2a5282; }"
            "QPushButton:pressed { background:#122540; }"
        )
        btn_save.clicked.connect(self._save)
        btn_row.addWidget(btn_save)

        root.addLayout(btn_row)

    # ------------------------------------------------------------------ #
    #  数据加载 / 预设                                                       #
    # ------------------------------------------------------------------ #
    def _load_from_cfg(self):
        laser_list = self.cfg.get("lasers", _DEFAULT_LASERS)
        self.table.setRowCount(0)
        for item in laser_list:
            self._add_row(item.get("tab_name", ""), item.get("device_id", ""))
        self.host_edit.setText(self.cfg.get("tcp_host", DEFAULT_CONFIG["tcp_host"]))
        self.port_edit.setText(str(self.cfg.get("tcp_port", DEFAULT_CONFIG["tcp_port"])))

    def _load_preset(self, preset: list):
        self.table.setRowCount(0)
        for item in preset:
            self._add_row(item.get("tab_name", ""), item.get("device_id", ""))

    def _add_row(self, tab_name: str = "", device_id: str = ""):
        row = self.table.rowCount()
        self.table.insertRow(row)

        name_item = QTableWidgetItem(tab_name)
        id_item   = QTableWidgetItem(device_id)
        self.table.setItem(row, 0, name_item)
        self.table.setItem(row, 1, id_item)

        del_btn = QPushButton("删除")
        del_btn.setStyleSheet(
            "QPushButton { color:#c0392b; border:none; font-family:微软雅黑; font-size:12px; }"
            "QPushButton:hover { color:#e74c3c; text-decoration:underline; }"
        )
        del_btn.clicked.connect(lambda _, r=row: self._delete_row(del_btn))
        self.table.setCellWidget(row, 2, del_btn)

    def _delete_row(self, btn: QPushButton):
        for row in range(self.table.rowCount()):
            if self.table.cellWidget(row, 2) is btn:
                self.table.removeRow(row)
                return

    # ------------------------------------------------------------------ #
    #  保存                                                                 #
    # ------------------------------------------------------------------ #
    def _save(self):
        lasers = []
        for row in range(self.table.rowCount()):
            tab_item = self.table.item(row, 0)
            id_item  = self.table.item(row, 1)
            tab_name  = tab_item.text().strip() if tab_item else ""
            device_id = id_item.text().strip()  if id_item  else ""
            if not tab_name and not device_id:
                continue
            if not device_id:
                device_id = tab_name
            lasers.append({"tab_name": tab_name, "device_id": device_id})

        if not lasers:
            QMessageBox.warning(self, "提示", "至少需要配置一台激光器")
            return

        host = self.host_edit.text().strip()
        port_text = self.port_edit.text().strip()
        if not host:
            QMessageBox.warning(self, "提示", "请填写 TCP 监听地址")
            return
        try:
            port = int(port_text)
        except ValueError:
            QMessageBox.warning(self, "提示", "TCP 端口号无效")
            return

        self.cfg["lasers"]   = lasers
        self.cfg["tcp_host"] = host
        self.cfg["tcp_port"] = port
        save_config(self.cfg)
        self.accept()


# ── 主窗口 ──────────────────────────────────────────────────────────────── #
class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("TSL-ITU-B  程控激光器控制")
        self.setFixedWidth(600)

        self.cfg = load_config()

        try:
            self.wl_list, self.wl_table = load_wavelength_table()
        except Exception as e:
            QMessageBox.critical(self, "错误", f"无法加载波长表：{e}")
            self.wl_list, self.wl_table = [], {}
        self.chn_to_wl = build_chn_to_wl(self.wl_table)

        self._build_ui()

        # 使用配置中的 IP/端口启动 TCP 服务器
        tcp_host = self.cfg.get("tcp_host", DEFAULT_CONFIG["tcp_host"])
        tcp_port = self.cfg.get("tcp_port", DEFAULT_CONFIG["tcp_port"])
        self.server = TCPServer(address=tcp_host, port=tcp_port)
        self.server.message_signal.connect(self._on_tcp_message)
        self.server.start()

    # ------------------------------------------------------------------ #
    #  UI 构建                                                              #
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setSpacing(0)
        root_layout.setContentsMargins(0, 0, 0, 0)

        # ── 标题栏 ────────────────────────────────────────────────────── #
        title_widget = QWidget()
        title_widget.setFixedHeight(48)
        title_widget.setStyleSheet("background:#1a3a5c;")
        title_layout = QHBoxLayout(title_widget)
        title_layout.setContentsMargins(16, 0, 8, 0)
        title_layout.setSpacing(8)

        title_label = QLabel("TSL-ITU-B  程控激光器控制")
        title_label.setFont(QFont("微软雅黑", 14, QFont.Bold))
        title_label.setStyleSheet("color:white; background:transparent;")
        title_layout.addWidget(title_label)
        title_layout.addStretch()

        btn_settings = QPushButton("设置")
        btn_settings.setFixedSize(80, 30)
        btn_settings.setFont(QFont("微软雅黑", 10))
        btn_settings.setStyleSheet(
            "QPushButton { background:rgba(255,255,255,0.15); color:white;"
            "  border:1px solid rgba(255,255,255,0.35); border-radius:4px; }"
            "QPushButton:hover  { background:rgba(255,255,255,0.28); }"
            "QPushButton:pressed{ background:rgba(255,255,255,0.08); }"
        )
        btn_settings.clicked.connect(self._open_config)
        title_layout.addWidget(btn_settings)

        root_layout.addWidget(title_widget)

        # ── 选项卡 ────────────────────────────────────────────────────── #
        self.tab_widget = QTabWidget()
        self.tab_widget.setStyleSheet(
            "QTabBar::tab { min-width: 100px; min-height: 28px; font-size: 13px; font-family: 微软雅黑; }"
            "QTabBar::tab:selected { font-weight: bold; color: #1a3a5c; }"
        )

        # 从配置动态创建激光器面板；device_id 大写后作为路由键
        self.panels: dict[str, LaserPanel] = {}
        laser_list = self.cfg.get("lasers", _DEFAULT_LASERS)
        for idx, laser_cfg in enumerate(laser_list):
            tab_name    = laser_cfg.get("tab_name",    f"激光器 {idx + 1}")
            device_id   = laser_cfg.get("device_id",   tab_name)
            port_cfg_key = laser_cfg.get("port_cfg_key", f"last_port_{device_id}")
            panel = LaserPanel(
                label=tab_name,
                wl_list=self.wl_list,
                wl_table=self.wl_table,
                chn_to_wl=self.chn_to_wl,
                port_cfg_key=port_cfg_key,
                cfg=self.cfg,
            )
            self.panels[device_id.upper()] = panel
            self.tab_widget.addTab(panel, tab_name)

        root_layout.addWidget(self.tab_widget)

    # ------------------------------------------------------------------ #
    #  TCP 远程控制协议处理                                                   #
    # ------------------------------------------------------------------ #
    def _on_tcp_message(self, client_socket, message: str):
        """
        处理远程控制 TCP 消息。
        所有指令均可携带 "device" 字段以指定目标设备。
        LaserON / LaserOFF 支持 "device": "ALL" 同时控制所有已连接激光器。
        不携带时默认路由到第一台激光器。
        """
        def reply(ok: bool, value=None, error: str = "Null"):
            self.server.back_signal.emit(
                client_socket, [ok, value if value is not None else "", error]
            )

        try:
            data = json.loads(message)
        except json.JSONDecodeError as e:
            reply(False, error=f"JSON解析错误: {e}")
            return

        opcode    = data.get("opcode", "")
        parameter = data.get("parameter", "")

        # device 字段从 parameter 中读取，大写后与 panels 字典的键匹配
        if isinstance(parameter, dict):
            device_field = str(parameter.get("device", "")).strip().upper()
        else:
            device_field = ""

        # 根据 device 字段路由到对应面板；未指定时默认路由到第一台激光器
        panel = self.panels.get(device_field) or next(iter(self.panels.values()))
        device_log = device_field or next(iter(self.panels))
        log_panel = next(iter(self.panels.values()))
        log_panel._log(f"[TCP] 收到指令: {opcode}  (device={device_log})")

        try:
            # ── 9.1 连接设备 ──────────────────────────────────────────── #
            if opcode == "ConnectDevice":
                if panel.device is not None:
                    reply(True)
                else:
                    reply(False, error=f"{panel.label} 未连接，请先通过串口连接设备")

            # ── 9.2 检查版本号 ─────────────────────────────────────────── #
            elif opcode == "check":
                reply(True, value=VERSION)

            # ── 8.1 开激光器 ──────────────────────────────────────────── #
            elif opcode == "LaserON":
                if device_field == "ALL":
                    panels_snap = list(self.panels.values())
                    if not any(p.device for p in panels_snap):
                        reply(False, error="没有已连接的激光器")
                        return
                    def _do_all_on(panels=panels_snap):
                        operated, failed = 0, []
                        for p in panels:
                            if p.device is None:
                                p._log(f"[TCP] LaserON 跳过: {p.label} 未连接")
                                continue
                            ok = p.cmd_and_wait(lambda dev=p.device: dev.cmd_output(True), OUTPUT_ADDR)
                            if ok:
                                p._log("[TCP] LaserON 执行成功")
                                operated += 1
                            else:
                                p._log(f"[TCP] LaserON 硬件应答超时: {p.label}")
                                failed.append(p.label)
                        if operated == 0:
                            reply(False, error="所有设备均未收到硬件应答")
                        elif failed:
                            reply(False, error=f"部分设备应答超时: {failed}")
                        else:
                            reply(True)
                    threading.Thread(target=_do_all_on, daemon=True).start()
                else:
                    if panel.device is None:
                        reply(False, error=f"{panel.label} 未连接")
                        return
                    def _do_on(p=panel):
                        ok = p.cmd_and_wait(lambda: p.device.cmd_output(True), OUTPUT_ADDR)
                        if ok:
                            p._log("[TCP] LaserON 执行成功")
                            reply(True)
                        else:
                            p._log("[TCP] LaserON 硬件应答超时")
                            reply(False, error="硬件应答超时")
                    threading.Thread(target=_do_on, daemon=True).start()

            # ── 8.2 关激光器 ──────────────────────────────────────────── #
            elif opcode == "LaserOFF":
                if device_field == "ALL":
                    panels_snap = list(self.panels.values())
                    if not any(p.device for p in panels_snap):
                        reply(False, error="没有已连接的激光器")
                        return
                    def _do_all_off(panels=panels_snap):
                        operated, failed = 0, []
                        for p in panels:
                            if p.device is None:
                                p._log(f"[TCP] LaserOFF 跳过: {p.label} 未连接")
                                continue
                            ok = p.cmd_and_wait(lambda dev=p.device: dev.cmd_output(False), OUTPUT_ADDR)
                            if ok:
                                p._log("[TCP] LaserOFF 执行成功")
                                operated += 1
                            else:
                                p._log(f"[TCP] LaserOFF 硬件应答超时: {p.label}")
                                failed.append(p.label)
                        if operated == 0:
                            reply(False, error="所有设备均未收到硬件应答")
                        elif failed:
                            reply(False, error=f"部分设备应答超时: {failed}")
                        else:
                            reply(True)
                    threading.Thread(target=_do_all_off, daemon=True).start()
                else:
                    if panel.device is None:
                        reply(False, error=f"{panel.label} 未连接")
                        return
                    def _do_off(p=panel):
                        ok = p.cmd_and_wait(lambda: p.device.cmd_output(False), OUTPUT_ADDR)
                        if ok:
                            p._log("[TCP] LaserOFF 执行成功")
                            reply(True)
                        else:
                            p._log("[TCP] LaserOFF 硬件应答超时")
                            reply(False, error="硬件应答超时")
                    threading.Thread(target=_do_off, daemon=True).start()

            # ── 8.3 调整波长 ──────────────────────────────────────────── #
            elif opcode == "SetWavelength":
                if panel.device is None:
                    reply(False, error=f"{panel.label} 未连接")
                    return
                wl = str(parameter.get("Wavelength", "")).strip() if isinstance(parameter, dict) else ""
                if wl == "1540":
                    wl = "1540.56"
                elif wl == "1550":
                    wl = "1550.12"
                elif wl == "1563":
                    wl = "1563.05"
                if not wl:
                    reply(False, error="参数 Wavelength 缺失")
                    return
                chn = self.wl_table.get(wl)
                if chn is None:
                    reply(False, error=f"未找到波长 {wl} 对应的通道号")
                    return
                def _do_wl(p=panel, ch=chn, wavelength=wl):
                    ok = p.cmd_and_wait(lambda: p.device.cmd_channel(ch), CHANNEL_ADDR)
                    if ok:
                        p._log(f"[TCP] SetWavelength {wavelength} nm (通道 {ch}) 执行成功")
                        reply(True)
                    else:
                        p._log(f"[TCP] SetWavelength {wavelength} nm 硬件应答超时")
                        reply(False, error="硬件应答超时")
                threading.Thread(target=_do_wl, daemon=True).start()

            # ── 8.4 获取波长 ──────────────────────────────────────────── #
            elif opcode == "GetWavelength":
                if panel.device is None:
                    reply(False, error=f"{panel.label} 未连接")
                    return
                chn = panel.get_channel_via_listener()
                wl = self.chn_to_wl.get(chn, f"通道{chn}") if chn is not None else ""
                panel._log(f"[TCP] GetWavelength 返回: {wl}")
                reply(True, value={"Wavelength": wl})

            # ── 8.5 调整功率 ──────────────────────────────────────────── #
            elif opcode == "SetPower":
                if panel.device is None:
                    reply(False, error=f"{panel.label} 未连接")
                    return
                try:
                    power = float(parameter.get("Power", 0)) if isinstance(parameter, dict) else float(parameter)
                except (TypeError, ValueError):
                    reply(False, error="参数 Power 无效")
                    return
                panel.device.cmd_power(power)
                panel._log(f"[TCP] SetPower {power:.2f} dBm 执行成功")
                reply(True)

            # ── 8.6 获取功率 ──────────────────────────────────────────── #
            elif opcode == "GetPower":
                if panel.device is None:
                    reply(False, error=f"{panel.label} 未连接")
                    return
                pw = panel.get_power_via_listener()
                panel._log(f"[TCP] GetPower 返回: {pw}")
                reply(True, value={"Power": pw})

            else:
                panel._log(f"[TCP] 未知指令: {opcode}")
                reply(False, error=f"未知指令: {opcode}")

        except Exception as e:
            panel._log(f"[TCP] 执行指令 {opcode} 异常: {e}")
            reply(False, error=str(e))

    # ------------------------------------------------------------------ #
    #  配置对话框                                                            #
    # ------------------------------------------------------------------ #
    def _open_config(self):
        dlg = ConfigDialog(self.cfg, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            self._apply_config()

    def _apply_config(self):
        """断开所有设备，根据当前 cfg 重建激光器面板"""
        for panel in self.panels.values():
            panel._disconnect()

        while self.tab_widget.count():
            self.tab_widget.removeTab(0)
        self.panels.clear()

        laser_list = self.cfg.get("lasers", _DEFAULT_LASERS)
        for idx, laser_cfg in enumerate(laser_list):
            tab_name     = laser_cfg.get("tab_name",     f"激光器 {idx + 1}")
            device_id    = laser_cfg.get("device_id",    tab_name)
            port_cfg_key = laser_cfg.get("port_cfg_key", f"last_port_{device_id}")
            panel = LaserPanel(
                label=tab_name,
                wl_list=self.wl_list,
                wl_table=self.wl_table,
                chn_to_wl=self.chn_to_wl,
                port_cfg_key=port_cfg_key,
                cfg=self.cfg,
            )
            self.panels[device_id.upper()] = panel
            self.tab_widget.addTab(panel, tab_name)

    def closeEvent(self, event):
        for panel in self.panels.values():
            panel._disconnect()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
