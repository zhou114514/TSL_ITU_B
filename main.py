"""
TSL-ITU-B 程控激光器控制软件  (PyQt5)
"""

import csv
import os
import sys
import threading
from datetime import datetime

import serial.tools.list_ports
from PyQt5.QtCore import Qt, pyqtSignal, QObject
from PyQt5.QtGui import QFont, QDoubleValidator
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QLabel, QComboBox, QPushButton, QLineEdit,
    QTextEdit, QMessageBox, QSizePolicy,
)

from TSL_ITU_B import TSL_ITU_B


# ── 线程信号桥 ──────────────────────────────────────────────────────────── #
class _Signals(QObject):
    log = pyqtSignal(str)
    output_state_changed = pyqtSignal(bool)


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


class MainWindow(QMainWindow):
    QUICK_WL = ["1540.56", "1563.05"]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("TSL-ITU-B  程控激光器控制")
        self.setFixedWidth(520)

        self.device: TSL_ITU_B | None = None
        self.output_on = False

        self._sig = _Signals()
        self._sig.log.connect(self._append_log)
        self._sig.output_state_changed.connect(self._update_output_button)

        try:
            self.wl_list, self.wl_table = load_wavelength_table()
        except Exception as e:
            QMessageBox.critical(self, "错误", f"无法加载波长表：{e}")
            self.wl_list, self.wl_table = [], {}

        self._build_ui()
        self._set_controls_enabled(False)

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

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setSpacing(10)
        body_layout.setContentsMargins(14, 12, 14, 12)
        root_layout.addWidget(body)

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
        body_layout.addWidget(conn_group)

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

        body_layout.addWidget(wl_group)

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

        body_layout.addWidget(pw_group)

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

        body_layout.addWidget(out_group)

        # ── 操作日志区 ────────────────────────────────────────────────── #
        log_group = QGroupBox("操作日志")
        log_layout = QVBoxLayout(log_group)

        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setFont(QFont("Consolas", 9))
        self.log_edit.setFixedHeight(160)
        log_layout.addWidget(self.log_edit)

        body_layout.addWidget(log_group)

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
            self.status_label.setText("已连接")
            self.status_label.setStyleSheet("color:green; font-weight:bold;")
            self.btn_connect.setText("断开")
            self._set_controls_enabled(True)
            self._log(f"已连接到 {port}")
        except Exception as e:
            self.device = None
            QMessageBox.critical(self, "连接失败", str(e))
            self._log(f"连接失败: {e}")

    def _disconnect(self):
        if self.device:
            try:
                if self.output_on:
                    self.device.set_output(False)
                    self.output_on = False
                self.device.disconnect()
            except Exception:
                pass
            self.device = None
        self.status_label.setText("未连接")
        self.status_label.setStyleSheet("color:red; font-weight:bold;")
        self.btn_connect.setText("连接")
        self._set_controls_enabled(False)
        self._update_output_button(False)
        self._log("已断开连接")

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
        self._run_in_thread(self._do_set_channel, wl, chn)

    def _quick_set_wavelength(self, wl: str):
        chn = self.wl_table.get(wl)
        if chn is None:
            QMessageBox.critical(self, "错误", f"未找到波长 {wl} 对应的通道号")
            return
        idx = self.wl_combo.findText(wl)
        if idx >= 0:
            self.wl_combo.setCurrentIndex(idx)
        self._run_in_thread(self._do_set_channel, wl, chn)

    def _do_set_channel(self, wl: str, chn: int):
        try:
            ok = self.device.set_channel(chn)
            if ok:
                self._log(f"波长设置成功: {wl} nm  (通道 {chn})")
            else:
                self._log(f"波长设置失败: {wl} nm  (通道 {chn})")
        except Exception as e:
            self._log(f"波长设置异常: {e}")

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
        self._run_in_thread(self._do_set_power, power)

    def _do_set_power(self, power: float):
        try:
            ok = self.device.set_power(power)
            if ok:
                self._log(f"功率设置成功: {power:.2f} dBm")
            else:
                self._log(f"功率设置失败: {power:.2f} dBm")
        except Exception as e:
            self._log(f"功率设置异常: {e}")

    # ------------------------------------------------------------------ #
    #  输出控制                                                             #
    # ------------------------------------------------------------------ #
    def _toggle_output(self):
        self._run_in_thread(self._do_toggle_output)

    def _do_toggle_output(self):
        try:
            target = not self.output_on
            ok = self.device.set_output(target)
            if ok:
                self.output_on = target
                self._sig.output_state_changed.emit(target)
                self._log("激光输出已开启" if target else "激光输出已关闭")
            else:
                self._log("输出控制指令未收到应答")
        except Exception as e:
            self._log(f"输出控制异常: {e}")

    def _update_output_button(self, state: bool):
        self._apply_output_style(state)

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

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self._sig.log.emit(f"[{ts}]  {msg}")

    def _append_log(self, line: str):
        self.log_edit.append(line)
        self.log_edit.verticalScrollBar().setValue(
            self.log_edit.verticalScrollBar().maximum()
        )

    def closeEvent(self, event):
        self._disconnect()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
