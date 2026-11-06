"""Protocolo de texto con checksum entre PC y ESP32.

Línea = PAYLOAD '*' HH '\\n'   (HH = XOR de los bytes de PAYLOAD, 2 hex mayúsculas)

PC -> ESP32:  PING | CFG,mask,intervalo_us,1 | START | STOP | STATUS
ESP32 -> PC:  HELLO,fw,boot_id,nmax        (al arrancar)
              ACK,<cmd>[,args]  /  NAK,<cmd>,<motivo>
              D,seq,t_us,ovr,mask,ch1,adc1,...,chN,adcN
  seq   contador uint32 de tramas (incrementa aunque la trama se descarte por TX lleno)
  t_us  micros() del ESP32, uint32 (desborda a los ~71.6 min)
  ovr   muestreos atrasados acumulados (el ESP32 no llegó a tiempo)
  adcK  lectura cruda 0..4095 (una sola analogRead por sensor; el campo CFG final es siempre 1)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

GPIOS = (32, 33, 34, 35, 36, 39)
MAX_SENSORS = 6
ADC_MAX = 4095
MIN_RATE_HZ = 0.1
MAX_RATE_HZ = 1000.0
MAX_OVERSAMPLE = 1   # campo CFG heredado: el firmware hace UNA lectura por sensor
MAX_LINE = 120
U32 = 2 ** 32
# Estimación conservadora del tiempo de una analogRead() del ESP32 (us); se mide en firmware con 'ovr'.
ADC_READ_US = 60
MAX_BANDWIDTH_UTIL = 0.5   # fracción del ancho de banda serie (10 bits/byte)


class ProtocolError(ValueError):
    pass


class ConfigError(ValueError):
    pass


def checksum(payload: str) -> int:
    c = 0
    for b in payload.encode("ascii", "replace"):
        c ^= b
    return c


def encode(payload: str) -> bytes:
    return f"{payload}*{checksum(payload):02X}\n".encode("ascii")


def decode(line) -> str:
    if isinstance(line, (bytes, bytearray)):
        if len(line) > MAX_LINE + 6:
            raise ProtocolError("línea demasiado larga")
        try:
            line = bytes(line).decode("ascii")
        except UnicodeDecodeError:
            raise ProtocolError("bytes no ASCII")
    line = line.strip("\r\n")
    if len(line) > MAX_LINE + 6:
        raise ProtocolError("línea demasiado larga")
    if not line:
        raise ProtocolError("línea vacía")
    idx = line.rfind("*")
    if idx <= 0 or len(line) - idx - 1 != 2:
        raise ProtocolError("sin checksum válido")
    payload, cs = line[:idx], line[idx + 1:]
    try:
        expected = int(cs, 16)
    except ValueError:
        raise ProtocolError("checksum no hexadecimal")
    if expected != checksum(payload):
        raise ProtocolError("checksum incorrecto")
    return payload


@dataclass(frozen=True)
class DataFrame:
    seq: int
    t_us: int
    ovr: int
    mask: int
    channels: tuple
    adc: tuple


@dataclass
class Message:
    kind: str
    fields: list
    frame: DataFrame | None = None


def _uint(s: str, name: str, max_v: int = U32 - 1) -> int:
    if not s.isdigit():
        raise ProtocolError(f"{name} no es entero sin signo: {s!r}")
    v = int(s)
    if v > max_v:
        raise ProtocolError(f"{name} fuera de rango")
    return v


def channels_from_mask(mask: int) -> tuple[int, ...]:
    if isinstance(mask, bool) or not isinstance(mask, int) or not 1 <= mask < (1 << MAX_SENSORS):
        raise ConfigError("La selección debe habilitar entre 1 y 6 sensores.")
    return tuple(i + 1 for i in range(MAX_SENSORS) if mask & (1 << i))


def mask_from_channels(channels) -> int:
    mask = 0
    try:
        values = tuple(int(c) for c in channels)
    except (TypeError, ValueError):
        raise ConfigError("Selección de sensores inválida.")
    if len(set(values)) != len(values) or not values or any(c < 1 or c > MAX_SENSORS for c in values):
        raise ConfigError("Seleccione entre 1 y 6 sensores distintos.")
    for c in values:
        mask |= 1 << (c - 1)
    return mask


def parse_data(payload: str, expected_mask: int | None = None) -> DataFrame:
    f = payload.split(",")
    if len(f) < 7 or (len(f) - 5) % 2:
        raise ProtocolError("trama D incompleta")
    seq = _uint(f[1], "seq")
    t_us = _uint(f[2], "t_us")
    ovr = _uint(f[3], "ovr")
    mask = _uint(f[4], "mask", (1 << MAX_SENSORS) - 1)
    if mask == 0 or (expected_mask is not None and mask != expected_mask):
        raise ProtocolError(f"máscara de canales {mask} inesperada")
    expected_channels = tuple(i + 1 for i in range(MAX_SENSORS) if mask & (1 << i))
    channels = []
    vals = []
    for pos in range(5, len(f), 2):
        ch = _uint(f[pos], "canal", MAX_SENSORS)
        if ch < 1:
            raise ProtocolError("canal fuera de rango")
        channels.append(ch)
        s = f[pos + 1]
        try:
            v = float(s)
        except ValueError:
            raise ProtocolError(f"ADC no numérico: {s!r}")
        if not math.isfinite(v) or v < 0 or v > ADC_MAX:
            raise ProtocolError(f"ADC fuera de rango: {s!r}")
        if s.startswith(("+", "-")) or s.lower().startswith(("0x",)):
            raise ProtocolError("formato ADC inválido")
        vals.append(v)
    if tuple(channels) != expected_channels:
        raise ProtocolError("mapa de canales no coincide con la máscara")
    return DataFrame(seq, t_us, ovr, mask, tuple(channels), tuple(vals))


def parse_line(line, expected_mask: int | None = None) -> Message:
    payload = decode(line)
    f = payload.split(",")
    kind = f[0]
    if kind == "D":
        return Message("D", f, parse_data(payload, expected_mask))
    if kind in ("ACK", "NAK", "HELLO", "STATUS", "ERR"):
        return Message(kind, f)
    raise ProtocolError(f"tipo de mensaje desconocido: {kind!r}")


@dataclass
class ConfigPlan:
    mask: int
    n: int
    rate_hz: float
    interval_us: int
    oversample: int
    baud: int
    frame_bytes: int
    bytes_per_s: float
    utilisation: float
    tick_us_estimate: int
    warnings: list = field(default_factory=list)


def max_frame_bytes(n: int) -> int:
    # "D,4294967295,4294967295,4294967295," + n * "4095.99," + "*XX\n"
    return 2 + 10 + 1 + 10 + 1 + 10 + 1 + 2 + n * 10 + 4


def plan_config(selection, rate_hz, oversample, baud) -> ConfigPlan:
    """Valida la configuración (Hz por sensor) y devuelve el plan; lanza ConfigError en español."""
    if isinstance(selection, int) and not isinstance(selection, bool):
        # Compatibilidad de API: un entero 1..6 sigue significando “primeros n”.
        if not 1 <= selection <= MAX_SENSORS:
            raise ConfigError(f"La cantidad de sensores debe ser un entero entre 1 y {MAX_SENSORS}.")
        channels = tuple(range(1, selection + 1))
        mask = mask_from_channels(channels)
    else:
        mask = mask_from_channels(selection)
        channels = channels_from_mask(mask)
    n = len(channels)
    if isinstance(oversample, bool) or not isinstance(oversample, int) or not 1 <= oversample <= MAX_OVERSAMPLE:
        raise ConfigError("El campo heredado CFG de promediado debe ser exactamente 1 (una lectura por sensor).")
    try:
        rate = float(rate_hz)
        baud = int(baud)
    except (TypeError, ValueError):
        raise ConfigError("Frecuencia o baudios no numéricos.")
    if not math.isfinite(rate) or not MIN_RATE_HZ <= rate <= MAX_RATE_HZ:
        raise ConfigError(f"La frecuencia por sensor debe estar entre {MIN_RATE_HZ} y {MAX_RATE_HZ:g} Hz.")
    if baud < 9600:
        raise ConfigError("Baudios demasiado bajos.")
    interval = int(round(1e6 / rate))
    tick = n * oversample * ADC_READ_US + 200
    if interval < 1.5 * tick:
        raise ConfigError(
            f"Intervalo de {interval} µs insuficiente: {n} sensores × {oversample} lecturas necesitan "
            f"≈{tick} µs por ciclo (se exige margen 1.5×). Baje la frecuencia.")
    fb = max_frame_bytes(n)
    bps = rate * fb
    util = bps / (baud / 10.0)
    if util > MAX_BANDWIDTH_UTIL:
        raise ConfigError(
            f"Ancho de banda serie insuficiente: {bps:.0f} B/s de {baud/10:.0f} B/s disponibles "
            f"({util*100:.0f} % > {MAX_BANDWIDTH_UTIL*100:.0f} %). Suba baudios o baje la frecuencia.")
    p = ConfigPlan(mask, n, rate, interval, oversample, baud, fb, bps, util, tick)
    if rate > 200:
        p.warnings.append("Frecuencia alta: aumenta el tamaño del CSV y la carga de la PC.")
    if util > 0.25:
        p.warnings.append("Uso del enlace serie superior al 25 %.")
    return p


@dataclass
class TrackResult:
    status: str          # first | ok | gap | duplicate | reboot
    lost: int = 0
    t_us_unwrapped: int = 0
    boot_epoch: int = 0
    rollover: bool = False


class SeqTracker:
    """Detecta pérdidas (seq), desborde de micros(), reinicios y duplicados."""

    def __init__(self):
        self.last_seq = None
        self.last_t = None
        self.wraps = 0
        self.boot_epoch = 0
        self._hello = False

    def note_hello(self):
        self._hello = True

    def update(self, seq: int, t_us: int) -> TrackResult:
        if self.last_seq is None:
            self.last_seq, self.last_t = seq, t_us
            self._hello = False
            return TrackResult("first", 0, t_us, self.boot_epoch)
        diff = (seq - (self.last_seq + 1)) % U32
        forward = diff < 2 ** 31
        t_back = t_us < self.last_t
        t_wrap = t_back and (self.last_t - t_us) > 2 ** 31
        reboot = self._hello or (not forward and t_back and not t_wrap) or (forward and diff > 0 and t_back and not t_wrap)
        if reboot:
            self._hello = False
            self.boot_epoch += 1
            self.wraps = 0
            self.last_seq, self.last_t = seq, t_us
            return TrackResult("reboot", 0, t_us, self.boot_epoch)
        if not forward:
            return TrackResult("duplicate", 0, self.wraps * U32 + self.last_t, self.boot_epoch)
        rollover = t_wrap
        if rollover:
            self.wraps += 1
        self.last_seq, self.last_t = seq, t_us
        return TrackResult("gap" if diff else "ok", diff, self.wraps * U32 + t_us, self.boot_epoch, rollover)
