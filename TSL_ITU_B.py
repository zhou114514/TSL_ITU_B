"""
锐力光电科技有限公司 TSL-ITU-B系列 程控激光器通信协议
16进制发送，波特率9600，1停止位，无校验位，8数据位
数据格式（默认6字节）:
[HEAD1, HEAD2, ADDR, DATAH, DATAL, SUM]
HEAD:   设置 0x00 0x01
        查询 0x01 0x00
        接收 0x01 0x01
ADDR: 数据地址位 通道：0x01 功率：0x02 开断：0x03
DATAH: 数据高8位
DATAL: 数据低8位
SUM: 校验和 前五字节求和
"""

import serial
import time

WRITE_HEAD = [0x00, 0x01]
READ_HEAD = [0x01, 0x00]
RECEIVE_HEAD = [0x01, 0x01]

CHANNEL_ADDR = 0x01
POWER_ADDR = 0x02
OUTPUT_ADDR = 0x03

class TSL_ITU_B:
    def __init__(self, port, baudrate=9600, timeout=0.1):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser = serial.Serial(port, baudrate, timeout=timeout)

    def send_data(self, data: list[int]) -> bool:
        try:
            self.ser.write(bytes(data))
            return True
        except Exception as e:
            print(f"Error sending data: {e}")
            return False

    def receive_data(self) -> bytes | None:
        try:
            data = self.ser.read(6)
            return data if len(data) == 6 else None
        except Exception as e:
            print(f"Error receiving data: {e}")
            return None

    def sum_data(self, data) -> bytes:
        return sum(data[0:5])

    def connect(self):
        self.ser.open()
    
    def disconnect(self):
        self.ser.close()

    def cal_data(self, value) -> bytes:
        if isinstance(value, float):
            value = int(value * 100)
        value = value & 0xFFFF
        return bytes([value >> 8, value & 0xFF])

    def check_data(self, data) -> bool:
        if len(data) == 0:
            return False
        if self.sum_data(data) == data[-1]:
            return True
        else:
            return False
    
    def set_channel(self, channel: int) -> bool:
        # 设置波长，每一个通道对应一个波长
        if channel < 0 or channel > 96:
            raise ValueError("Channel must be between 0 and 96")
        data = [*WRITE_HEAD, CHANNEL_ADDR, *self.cal_data(channel)]
        data.append(self.sum_data(data))
        self.send_data(data)
        receive_data = self.receive_data()
        return self.check_data(receive_data)
    
    def get_channel(self) -> int | None:
        # 获取当前通道
        data = [*READ_HEAD, CHANNEL_ADDR, *self.cal_data(0)]
        data.append(self.sum_data(data))
        self.send_data(data)
        receive_data = self.receive_data()
        # print(receive_data.hex())
        if self.check_data(receive_data):
            return int.from_bytes(receive_data[3:5], 'big')
        else:
            return None
    
    def set_power(self, power: float) -> bool:
        # 设置功率，功率范围为7dbm到13dbm，步进0.01dbm
        if power < 7 or power > 13:
            raise ValueError("Power must be between 7 and 13")
        data = [*WRITE_HEAD, POWER_ADDR, *self.cal_data(power)]
        data.append(self.sum_data(data))
        self.send_data(data)
        receive_data = self.receive_data()
        return self.check_data(receive_data)
    
    def get_power(self) -> float | None:
        # 获取当前功率
        data = [*READ_HEAD, POWER_ADDR, *self.cal_data(0)]
        data.append(self.sum_data(data))
        self.send_data(data)
        receive_data = self.receive_data()
        # print(receive_data.hex())
        if self.check_data(receive_data):
            return int.from_bytes(receive_data[3:5], 'big') / 100
        else:
            return None
    
    def set_output(self, output: bool) -> bool:
        # 设置输出，输出为True或False
        if output not in [True, False]:
            raise ValueError("Output must be True or False")
        if output:
            output = 0x0101
        else:
            output = 0x0000
        data = [*WRITE_HEAD, OUTPUT_ADDR, *self.cal_data(output)]
        data.append(self.sum_data(data))
        self.send_data(data)
        receive_data = self.receive_data()
        return self.check_data(receive_data)

    def get_output(self) -> bool | None:
        # 获取当前输出
        data = [*READ_HEAD, OUTPUT_ADDR, *self.cal_data(0)]
        data.append(self.sum_data(data))
        self.send_data(data)
        receive_data = self.receive_data()
        # print(receive_data.hex())
        if self.check_data(receive_data):
            return True if receive_data[3:5] == b'\x01\x01' else False
        else:
            return None

    # ── 仅发送指令（不读取应答，由外部监听线程统一接收） ────────────────── #

    def cmd_channel(self, channel: int) -> None:
        """发送设置通道指令，不等待应答"""
        if channel < 0 or channel > 96:
            raise ValueError("Channel must be between 0 and 96")
        data = [*WRITE_HEAD, CHANNEL_ADDR, *self.cal_data(channel)]
        data.append(self.sum_data(data))
        self.send_data(data)

    def cmd_power(self, power: float) -> None:
        """发送设置功率指令，不等待应答"""
        if power < 7 or power > 13:
            raise ValueError("Power must be between 7 and 13")
        data = [*WRITE_HEAD, POWER_ADDR, *self.cal_data(power)]
        data.append(self.sum_data(data))
        self.send_data(data)

    def cmd_output(self, output: bool) -> None:
        """发送设置输出指令，不等待应答"""
        value = 0x0101 if output else 0x0000
        data = [*WRITE_HEAD, OUTPUT_ADDR, *self.cal_data(value)]
        data.append(self.sum_data(data))
        self.send_data(data)


if __name__ == "__main__":
    import time
    tsl = TSL_ITU_B('COM7')
    # tsl.connect()
    print(tsl.get_channel())
    time.sleep(0.1)
    print(tsl.get_power())
    time.sleep(0.1)
    print(tsl.get_output())
    time.sleep(0.1)
    tsl.disconnect()