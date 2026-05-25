"""
TSL-ITU-B 程控激光器控制软件  (PyQt5) — 双设备版本

支持同时管理两台激光器（发射端 / CCD端），通过选项卡切换操作界面。
远程控制协议在所有指令中新增 "device" 字段，用于指定目标设备：
    "Transmitter" → 发射端
    "CCD"         → CCD 端
若未携带 "device" 字段，默认路由到发射端。

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
from PyQt5.QtGui import QFont, QDoubleValidator
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QLabel, QComboBox, QPushButton, QLineEdit,
    QTextEdit, QMessageBox, QSizePolicy, QTabWidget,
)

from TSL_ITU_B import TSL_ITU_B, CHANNEL_ADDR, POWER_ADDR, OUTPUT_ADDR


# ── 配置文件 ─────────────────────────────────────────────────────────────── #
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
DEFAULT_CONFIG: dict = {
    "last_port_tx": "",
    "last_port_ccd": "",
    "tcp_host": "127.0.0.1",
    "tcp_port": 10009,
}


def load_config() -> dict:
    """加载配置文件，若文件不存在或解析失败则返回默认值"""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            cfg = dict(DEFAULT_CONFIG)
            cfg.update(data)
            # 兼容旧版 last_port 字段
            if "last_port" in data and not cfg["last_port_tx"]:
                cfg["last_port_tx"] = data["last_port"]
            return cfg
        except Exception:
            pass
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
    docs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
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


# ── 单设备控制面板 ──────────────────────────────────────────────────────── #
class LaserPanel(QWidget):
    """封装单台激光器的串口连接、状态显示、波长/功率/输出控制及操作日志"""

    QUICK_WL = ["1540.56", "1563.05"]

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
        self.wl_combo = QComboBox()
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
            t.start()
        except Exception as e:
            self.device = None
            QMessageBox.critical(self, "连接失败", str(e))
            self._log(f"[{self.label}] 连接失败: {e}")

    def _disconnect(self):
        self._stop_listener.set()
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
        title_bar = QLabel("TSL-ITU-B  程控激光器控制")
        title_bar.setAlignment(Qt.AlignCenter)
        title_bar.setFixedHeight(48)
        title_bar.setFont(QFont("微软雅黑", 14, QFont.Bold))
        title_bar.setStyleSheet("background:#1a3a5c; color:white;")
        root_layout.addWidget(title_bar)

        # ── 选项卡 ────────────────────────────────────────────────────── #
        self.tab_widget = QTabWidget()
        self.tab_widget.setStyleSheet(
            "QTabBar::tab { min-width: 100px; min-height: 28px; font-size: 13px; font-family: 微软雅黑; }"
            "QTabBar::tab:selected { font-weight: bold; color: #1a3a5c; }"
        )

        self.panel_tx = LaserPanel(
            label="发射端",
            wl_list=self.wl_list,
            wl_table=self.wl_table,
            chn_to_wl=self.chn_to_wl,
            port_cfg_key="last_port_tx",
            cfg=self.cfg,
        )
        self.panel_ccd = LaserPanel(
            label="CCD端",
            wl_list=self.wl_list,
            wl_table=self.wl_table,
            chn_to_wl=self.chn_to_wl,
            port_cfg_key="last_port_ccd",
            cfg=self.cfg,
        )

        self.tab_widget.addTab(self.panel_tx,  "发射端")
        self.tab_widget.addTab(self.panel_ccd, "CCD 端")

        root_layout.addWidget(self.tab_widget)

    # ------------------------------------------------------------------ #
    #  TCP 远程控制协议处理                                                   #
    # ------------------------------------------------------------------ #
    def _on_tcp_message(self, client_socket, message: str):
        """
        处理远程控制 TCP 消息。
        所有指令均可携带 "device" 字段（"Transmitter" / "CCD"）以指定目标设备。
        不携带时默认路由到发射端。
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

        # device 字段从 parameter 中读取
        if isinstance(parameter, dict):
            device_field = str(parameter.get("device", "")).strip().upper()
        else:
            device_field = ""

        # 根据 device 字段路由到对应面板
        if device_field == "CCD":
            panel = self.panel_ccd
        else:
            panel = self.panel_tx

        panel._log(f"[TCP] 收到指令: {opcode}  (device={device_field or 'Transmitter'})")

        try:
            # ── 9.1 连接设备 ──────────────────────────────────────────── #
            if opcode == "ConnectDevice":
                if panel.device is not None:
                    reply(True)
                else:
                    reply(False, error=f"{panel.label} 未连接，请先通过串口连接设备")

            # ── 9.2 检查版本号 ─────────────────────────────────────────── #
            elif opcode == "check":
                reply(True)

            # ── 8.1 开激光器 ──────────────────────────────────────────── #
            elif opcode == "LaserON":
                if panel.device is None:
                    reply(False, error=f"{panel.label} 未连接")
                    return
                panel.device.cmd_output(True)
                panel._log(f"[TCP] LaserON 执行成功")
                reply(True)

            # ── 8.2 关激光器 ──────────────────────────────────────────── #
            elif opcode == "LaserOFF":
                if panel.device is None:
                    reply(False, error=f"{panel.label} 未连接")
                    return
                panel.device.cmd_output(False)
                panel._log(f"[TCP] LaserOFF 执行成功")
                reply(True)

            # ── 8.3 调整波长 ──────────────────────────────────────────── #
            elif opcode == "SetWavelength":
                if panel.device is None:
                    reply(False, error=f"{panel.label} 未连接")
                    return
                wl = str(parameter.get("Wavelength", "")).strip() if isinstance(parameter, dict) else ""
                if not wl:
                    reply(False, error="参数 Wavelength 缺失")
                    return
                chn = self.wl_table.get(wl)
                if chn is None:
                    reply(False, error=f"未找到波长 {wl} 对应的通道号")
                    return
                panel.device.cmd_channel(chn)
                panel._log(f"[TCP] SetWavelength {wl} nm (通道 {chn}) 执行成功")
                reply(True)

            # ── 8.4 获取波长 ──────────────────────────────────────────── #
            elif opcode == "GetWavelength":
                if panel.device is None:
                    reply(False, error=f"{panel.label} 未连接")
                    return
                chn = panel.device.get_channel()
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
                pw = panel.device.get_power()
                panel._log(f"[TCP] GetPower 返回: {pw}")
                reply(True, value={"Power": pw})

            else:
                panel._log(f"[TCP] 未知指令: {opcode}")
                reply(False, error=f"未知指令: {opcode}")

        except Exception as e:
            panel._log(f"[TCP] 执行指令 {opcode} 异常: {e}")
            reply(False, error=str(e))

    def closeEvent(self, event):
        self.panel_tx._disconnect()
        self.panel_ccd._disconnect()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
