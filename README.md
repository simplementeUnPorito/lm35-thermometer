<h1 align="center">LM35 Thermometer</h1>
<p align="center"><b>Desktop acquisition, per-sensor calibration and logging for up to six LM35 sensors on an ESP32.</b><br>
<img src="https://img.shields.io/badge/Python-3.10%2B-3776ab"> <img src="https://img.shields.io/badge/ESP32-Arduino%20%7C%20PlatformIO-1f6feb"> <img src="https://img.shields.io/badge/LM35-10%20mV%2F%C2%B0C-orange"> <img src="https://img.shields.io/badge/Tests-unittest-brightgreen"></p>
<p align="center"><a href="#english">English</a> · <a href="#español">Español</a></p>

## English

### Overview
A Python desktop application that acquires, plots, calibrates and records up to six LM35 temperature sensors read by an ESP32. The ESP32 firmware only streams raw ADC values over a checksummed serial protocol; temperature conversion and calibration happen on the PC. Each session keeps a raw CSV, JSON metadata, an event log and copies of sensor placement photos. A DEMO mode runs everything without hardware, and its files are marked `SIMULADO`.

> Nominal temperature is only an approximation. The ESP32 ADC has tolerances and non-linearity: calibrate each sensor against a reference before treating values as measurements.

### Features
- Up to 6 channels (S1-S6), any combination, with a bit mask so unselected GPIOs are never sampled.
- Configurable sampling rate (default 100 Hz per sensor, 921600 baud); the GUI estimates the serial budget and rejects out-of-range settings. The protocol reports lost frames and late samples.
- Live plot (10 s window, 5 Hz refresh), frozen on stop, then zoom and pan over the whole recorded session. Causal moving-average smoothing (1-1000 samples) applies to the plot only; the CSV always stores raw ADC and temperature.
- Per-sensor least-squares calibration (`temperature_C = slope * raw_ADC + offset`) with live capture, manual points, import/export and recalculation of a raw CSV into a new file.
- Session traceability: raw CSV, `*_session.json`, `*_eventos.log` and per-session `photos/`.
- Cleanup tools for sessions, calibrations or all data, blocked during acquisition.

### Wiring
| Channel | ESP32 GPIO | ADC |
|---|---:|---|
| S1 | 32 | ADC1 |
| S2 | 33 | ADC1 |
| S3 | 34 | ADC1 |
| S4 | 35 | ADC1 |
| S5 | 36 (VP) | ADC1 |
| S6 | 39 (VN) | ADC1 |

Power each LM35 from **5 V** (it needs 4-30 V), connect its output to the assigned GPIO and share **GND** with the ESP32. Check the datasheet for your package pinout. The GPIO must never see more than 3.3 V; the LM35 output (10 mV/°C) stays below that in normal use. Use an isolated supply for lab setups and keep sensors and wires isolated from energized parts.

### Quick start
Requires Python 3.10 or later (Windows scripts provided).

```powershell
.\instalar.bat   # creates a local .venv and installs requirements
.\iniciar.bat    # starts the GUI
```

1. Select the serial port, or enable **Modo DEMO**.
2. Enter a free-text experiment label.
3. Tick the channels to sample, name each sensor and optionally attach a placement photo.
4. Press **Iniciar**. Changing channels, label, names or photos during acquisition cleanly closes the session and opens a new one.

Firmware (`esp32dev`, 921600 baud):

```powershell
pio run -d firmware
pio run -d firmware -t upload --upload-port COM3
```

Or open `firmware/lm35_stream/lm35_stream.ino` in the Arduino IDE (esp32 by Espressif Systems). Do not keep a serial monitor open while the app runs.

### Calibration
Use the **Calibración por sensor** tab: choose a sensor, enter the real temperature and press **Append medición de calibración**; during acquisition it pairs the reference with the average ADC of the last second. You can also add manual points, delete points, disable or reset the calibration of one sensor, and recalculate a raw CSV. Wait for thermal stability and use at least three points across the working range (minimum two). An IR gun measures surface temperature, so account for emissivity. The firmware uses 6 dB attenuation (useful range about 0.15-1.75 V) and the nominal conversion assumes a fixed 2.2 V full scale.

### Data format
Serial protocol (text lines, `PAYLOAD*HH`, HH = XOR checksum in hex), defined in `lm35/protocol.py`:

```
PC -> ESP32:  PING | CFG,mask,interval_us,1 | START | STOP | STATUS
ESP32 -> PC:  HELLO,fw,boot_id,nmax | ACK,cmd | NAK,cmd,reason
              D,seq,t_us,ovr,mask,ch1,adc1,...,chN,adcN
```

Each session lives under `datos/sesiones/`:
- `*_raw.csv`: one row per enabled channel and frame (ADC, °C, GPIO, name, label, relative photo path, calibration revision)
- `*_session.json`: configuration, channels, calibration/name snapshot, photos, statistics
- `*_eventos.log`: resets, losses, calibration changes, close
- `photos/`: per-session copies of the attached images

### Tests
```powershell
python -m unittest discover -s tests -t . -v
python -m lm35 demo --seconds 4 --inject-faults
python -m lm35 smoke --seconds 1.5
python -m tests.validar_hardware --port COM3   # with a real board; floating inputs are not temperature data
```
The suite covers calibration, cleanup, history, protocol, recalculation, smoothing, the worker thread and a GUI smoke test.

### Repository layout
```
lm35/       Python package (gui, worker, protocol, calibration, recorder, recalc, demo)
firmware/   ESP32 sketch and platformio.ini
tests/      unittest suite and hardware validation script
*.bat       Windows install / start / test helpers
```

### Team & credits
Developed by Elías David Álvarez Martínez at Universidad Católica "Nuestra Señora de la Asunción" (Paraguay).

### License
MIT.

---

## Español

### Descripción general
Aplicación de escritorio en Python para adquirir, graficar, calibrar y registrar hasta seis sensores LM35 leídos por un ESP32. El firmware solo transmite valores ADC crudos por un protocolo serie con checksum; la conversión a temperatura y la calibración se hacen en la PC. Cada sesión conserva CSV crudo, metadatos JSON, registro de eventos y copias de las fotos de los sensores. El modo DEMO funciona sin hardware y marca sus archivos como `SIMULADO`.

> La temperatura nominal es solo una aproximación: el ADC del ESP32 tiene tolerancias y no linealidades. Calibre cada sensor contra una referencia.

### Funciones
- Hasta 6 canales (S1-S6) en cualquier combinación; los GPIO desmarcados no se muestrean.
- Frecuencia configurable (100 Hz por sensor y 921600 baudios por defecto), con detección de tramas perdidas y muestreos atrasados.
- Gráfica en vivo (ventana de 10 s), congelada al detener y navegable con zoom y desplazamiento. El suavizado (1-1000 muestras) solo afecta la gráfica; el CSV guarda siempre ADC crudo.
- Calibración lineal por sensor (mínimos cuadrados), captura en vivo, puntos manuales, importación/exportación y recálculo de CSV crudo.
- Trazabilidad por sesión y herramientas de limpieza de datos.

### Conexión
Misma tabla de canales y GPIO que en la sección en inglés (S1-S6 en GPIO 32, 33, 34, 35, 36, 39). Alimente cada LM35 con 5 V, conecte su salida al GPIO asignado y una GND con el ESP32. El GPIO nunca debe superar 3,3 V.

### Inicio rápido
Requiere Python 3.10 o posterior. Ejecute `instalar.bat` y luego `iniciar.bat`. Seleccione el puerto o active **Modo DEMO**, escriba una etiqueta, marque los canales y pulse **Iniciar**. Firmware: `pio run -d firmware -t upload --upload-port COM3`, o el sketch `firmware/lm35_stream/lm35_stream.ino` desde Arduino IDE.

### Calibración
En la pestaña **Calibración por sensor**: elija el sensor, escriba la medición real y pulse **Append medición de calibración** (usa el promedio ADC del último segundo). Use al menos tres puntos en el rango de trabajo (mínimo dos) y espere estabilidad térmica.

### Formato de datos
El protocolo está documentado en `lm35/protocol.py`; los archivos de sesión son `*_raw.csv`, `*_session.json`, `*_eventos.log` y `photos/` (ver sección en inglés).

### Pruebas
`python -m unittest discover -s tests -t . -v`, `python -m lm35 demo --seconds 4 --inject-faults` y `python -m lm35 smoke --seconds 1.5`.

### Equipo y créditos
Desarrollado por Elías David Álvarez Martínez en la Universidad Católica "Nuestra Señora de la Asunción" (Paraguay).

### Licencia
MIT.
