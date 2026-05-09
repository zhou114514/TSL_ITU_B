import sys
import socket
from PyQt5.QtWidgets import QApplication, QMainWindow, QLabel, QVBoxLayout, QWidget, QPushButton, QLineEdit, QTextEdit
from PyQt5.QtCore import QThread, pyqtSignal, Qt
import threading

from PyQt5.QtCore import QThread, pyqtSignal, QTimer
import socket
import time
import pandas as pd
import os
import datetime
import json

class TCPServer(QThread):
    message_signal = pyqtSignal([socket.socket, str])
    back_signal = pyqtSignal([socket.socket, list])

    def __init__(self, address='127.0.0.1', port=5090):
        super(TCPServer, self).__init__()
        # 本机IP地址
        self.host = address
        self.port = int(port)
        self.back_signal.connect(self.send_callback)

    def send_callback(self, client_socket:socket.socket, data:list):
        """发送回调"""
        self.send(client_socket, self.make_pack(data))

    def handle_client_connection(self, client_socket:socket.socket):
        """处理客户端连接"""
        try:
            buffer = ""
            while True:
                data = client_socket.recv(1024).decode('utf-8')
                if not data:
                    break
                buffer += data
                if "\n" in buffer:
                    messages = buffer.split("\n")
                    for message in messages[:-1]:
                        # 处理客户端发送的数据
                        if message:
                            print(f"来自{client_socket.getpeername()}的消息: {message}")
                            # self.info_signal.emit(f"来自{client_socket.getpeername()}的消息: {message}")
                            self.message_signal.emit(client_socket, message)
                    buffer = messages[-1]
            if buffer:
                # 处理剩余数据
                self.message_signal.emit(client_socket, buffer)
        except Exception as e:
            print(f"{client_socket.getpeername()}:客户端连接异常: {e}")
            self.send(client_socket, self.make_pack([False, "", f"{e}"]))
        finally:
            print(f"关闭来自{client_socket.getpeername()}的连接")
            # self.client_threads.pop(client_socket.getpeername())  # 删除客户端线程
            client_socket.shutdown(socket.SHUT_RDWR)
            client_socket.close()

    def run(self):
        """启动TCP服务器"""
        print("启动TCP服务器")
        print(f"本机IP地址: {self.host}")
        print(f"端口号: {self.port}")

        # 检查链接是否使用
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            result = s.connect_ex((self.host, self.port))
            if result == 0:
                print(f"端口{self.port}已被占用，请更换端口")
                return

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
            server_socket.bind(('', self.port))
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_socket.listen(5)
            print(f"服务器正在{self.host}:{self.port}上监听...")

            while True:
                try:
                    client_socket, addr = server_socket.accept()
                    # self.clients[addr[0]] = client_socket
                    print(f"接受到来自{addr}的连接")
                    # 为每个客户端连接创建一个单独的线程来处理
                    client_thread = threading.Thread(target=self.handle_client_connection, args=(client_socket,))
                    # self.client_threads[addr] = client_thread
                    client_thread.start()
                    # print(self.client_threads)
                except Exception as e:
                    print(f"连接异常: {e}")

    def send(self, client_socket, data):
        """向客户端发送数据"""
        data = data + "\n"
        print(f"向{client_socket.getpeername()}发送数据: {data}")
        # self.info_signal.emit(f"向{client_socket.getpeername()}发送数据: {data}")
        try:
            client_socket.sendall(data.encode('utf-8'))
        except Exception as e:
            print(f"向{client_socket.getpeername()}发送数据异常: {e}")

    def make_pack(self, data:list):
        """打包数据"""
        return json.dumps({"IsSuccessful":data[0], "Value":data[1], "ErrorMessage":data[2]})

    def close_tcp_server(self):
        """关闭TCP服务器"""
        # 这里需要实现一个优雅的关闭机制，考虑到多线程情况
        pass

if __name__ == '__main__':
    app = QApplication(sys.argv)
    server = TCPServer()
    server.start()
    re = server.check_login_log(datetime.datetime(2025, 1, 14, 9, 30, 14), datetime.datetime(2025, 1, 14, 9, 30, 18), 4)
    print(re)
    sys.exit(app.exec_())