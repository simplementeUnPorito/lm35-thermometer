"""Dispositivo SIMULADO: imita el protocolo del firmware sobre un 'puerto serie' falso.

NO es hardware real. Todo lo que genere se marca como SIMULADO (nombres de archivo y columna 'demo').
Sirve para probar la GUI/CSV/calibración sin ESP32 y para verificar la detección de fallos
(pérdida de tramas, checksum malo, basura, desborde de micros(), reinicio, desconexión).
"""
from __future__ import annotations

import math
import random
import time

import serial

from . import protocol as P

DEMO_TRUE_VFS = 2.05     # el "ADC simulado" NO coincide con el nominal 2.2 V, para que la calibración tenga efecto
FW = "SIM-1.0"


def true_temp(i: int, t: float) -> float:
    """Temperatura 'real' simulada del sensor i (0-based) en el instante t [s]."""
    return 22.0 + 4.0 * i + 6.0 * math.sin(2 * math.pi * t / 15.0 + i)


class FakeSerial:
    def __init__(self, faults=False, disconnect_after_frames=None, clock=time.monotonic, t0_us=None, seed=1):
        self.timeout = 0.05
        self.is_open = True
        self._clock = clock
        self._rng = random.Random(seed)
        self._faults = faults
        self._disc = disconnect_after_frames
        self._emitted = 0
        self._t0_us = t0_us if t0_us is not None else (P.U32 - 300_000 if faults else 1000)
        self._done = set()
        self._out = bytearray()
        self._boot_count = 0
        self._boot()

    # --- modelo del firmware --------------------------------------------
    def _boot(self, t0=None):
        self._boot_count += 1
        self.boot_id = self._rng.randrange(1, 2 ** 31)
        self._boot_time = self._clock()
        self._base_us = self._t0_us if self._boot_count == 1 else 0
        self.cfg = None
        self.running = False
        self.seq = 0
        self.ticks = 0
        self.ovr = 0
        self._send(f"HELLO,{FW},{self.boot_id},{P.MAX_SENSORS}")

    def _send(self, payload: str, corrupt=False):
        b = bytearray(P.encode(payload))
        if corrupt:
            b[-2] = ord("0") if b[-2] != ord("0") else ord("1")
        self._out += b

    def _check_alive(self):
        if not self.is_open or (self._disc is not None and self._emitted >= self._disc):
            raise serial.SerialException("dispositivo desconectado (simulado)")

    def _command(self, line: bytes):
        try:
            f = P.decode(line).split(",")
        except P.ProtocolError:
            self._send("NAK,?,CHECKSUM")
            return
        c = f[0]
        if c == "PING":
            self._send(f"ACK,PING,{FW},{self.boot_id}")
        elif c == "CFG":
            try:
                mask, iv, os_ = int(f[1]), int(f[2]), int(f[3])
            except (IndexError, ValueError):
                self._send("NAK,CFG,FORMATO")
                return
            if self.running:
                self._send("NAK,CFG,OCUPADO")
            elif not (1 <= mask <= 63 and 1000 <= iv <= 10_000_000 and 1 <= os_ <= 16):
                self._send("NAK,CFG,RANGO")
            else:
                self.cfg = (mask, iv, os_)
                self._send(f"ACK,CFG,{mask},{iv},{os_}")
        elif c == "START":
            if not self.cfg:
                self._send("NAK,START,SIN_CFG")
            else:
                self.running, self.seq, self.ticks = True, 0, 0
                self._next = self._clock() + self.cfg[1] / 1e6
                self._send("ACK,START")
        elif c == "STOP":
            self.running = False
            self._send("ACK,STOP")
        elif c == "STATUS":
            self._send(f"STATUS,{int(self.running)},{self.seq},{self.ovr}")
        else:
            self._send(f"NAK,{c},DESCONOCIDO")

    def _advance(self):
        if not self.running:
            return
        now = self._clock()
        mask, iv, os_ = self.cfg
        while self.running and self._next <= now:
            tv = self._next - self._boot_time
            self.ticks += 1
            seq = self.seq
            self.seq = (self.seq + 1) % P.U32
            self._next += iv / 1e6
            if self._faults:
                k = self.ticks
                if k == 70 and "reboot" not in self._done:
                    self._done.add("reboot")
                    self._boot()
                    return
                if k == 15 and "drop" not in self._done:
                    self._done.add("drop")
                    continue
                if k == 40 and "junk" not in self._done:
                    self._done.add("junk")
                    self._out += b"\x00\xffbasura\n"
            t_us = (self._base_us + int(tv * 1e6)) % P.U32
            vals = []
            fields = []
            for i in range(P.MAX_SENSORS):
                if not (mask & (1 << i)):
                    continue
                v = 0.01 * true_temp(i, tv) * (1 + 0.01 * (i - 2))
                adc = v / DEMO_TRUE_VFS * P.ADC_MAX + 8 + self._rng.uniform(-1.5, 1.5)
                fields.extend((str(i + 1), str(int(min(max(round(adc), 0), P.ADC_MAX)))))
            corrupt = self._faults and self.ticks == 25 and "corrupt" not in self._done
            if corrupt:
                self._done.add("corrupt")
            self._send(f"D,{seq},{t_us},{self.ovr},{mask}," + ",".join(fields), corrupt=corrupt)
            self._emitted += 1

    # --- interfaz tipo pyserial -----------------------------------------
    @property
    def in_waiting(self):
        self._check_alive()
        self._advance()
        return len(self._out)

    def write(self, data: bytes):
        self._check_alive()
        for ln in bytes(data).splitlines():
            if ln.strip():
                self._command(ln)
        return len(data)

    def read(self, size=1):
        end = self._clock() + self.timeout
        while True:
            self._check_alive()
            self._advance()
            if self._out:
                chunk = bytes(self._out[:size])
                del self._out[:size]
                return chunk
            if self._clock() >= end:
                return b""
            time.sleep(0.002)

    def reset_input_buffer(self):
        self._out.clear()

    def close(self):
        self.is_open = False
