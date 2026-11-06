// LM35 x hasta 6 -> ESP32-WROOM-32U DevKit (Arduino-ESP32). Sin WiFi/Bluetooth.
// El firmware SOLO lee el ADC crudo y lo transmite; la conversion a °C y la calibracion se hacen en la PC.
//
// Pines (todos ADC1, solo entrada, validos con o sin WiFi):
//   S1=GPIO32  S2=GPIO33  S3=GPIO34  S4=GPIO35  S5=GPIO36(VP)  S6=GPIO39(VN)
// Atenuacion: ADC_6db  (rango nominal util ~0.15 .. 1.75 V; LM35 = 10 mV/°C => ~15 .. 175 °C teoricos,
//   en la practica ~15..150 °C porque la entrada NO debe superar ~2.2 V y por debajo de ~0.1-0.15 V el ADC
//   del ESP32 es poco lineal). Para 0-100 °C (0-1 V) 6 dB es un buen compromiso. La PC corrige con calibracion.
// Alimente el LM35 con 5 V (necesita 4..30 V; NO con 3.3 V) y GND comun con el ESP32. La alimentacion no fija la
//   plena escala del ADC (la fija la atenuacion). La entrada del ADC NUNCA debe superar 3.3 V: LM35 a 5 V da 1.5 V a 150 °C (seguro),
//   pero un cortocircuito/falla del LM35 podria subir mucho: use divisor/diodo/resistencia serie (ver README).
//
// Protocolo (texto, '\n', checksum XOR): ver lm35/protocol.py.
//   PC->ESP: PING | CFG,mask,intervalo_us,1 | START | STOP | STATUS
//   ESP->PC: ... | D,seq,t_us,ovr,mask,canal,adc,... (IDs estables; solo lee bits habilitados)

#include <Arduino.h>

static const char *FW = "LM35-ESP32-2.1";
static const uint8_t PINS[6] = {32, 33, 34, 35, 36, 39};
static const uint32_t BAUD = 921600;

static uint8_t channelMask = 1;
static uint32_t intervalUs = 10000;
static bool running = false;
static uint32_t seq = 0;
static uint32_t ovr = 0;        // muestreos atrasados acumulados
static uint32_t txDrop = 0;     // tramas descartadas por buffer TX lleno (el seq igual avanza -> la PC ve el hueco)
static uint32_t nextTick = 0;
static uint32_t bootId;

static char cmdBuf[96];
static uint8_t cmdLen = 0;

static uint8_t cksum(const char *s, size_t n) {
  uint8_t c = 0;
  for (size_t i = 0; i < n; i++) c ^= (uint8_t)s[i];
  return c;
}

static void sendLine(const char *payload) {
  char out[160];
  int n = snprintf(out, sizeof(out), "%s*%02X\n", payload, cksum(payload, strlen(payload)));
  if (n > 0 && Serial.availableForWrite() >= n) Serial.write((const uint8_t *)out, n);
  else txDrop++;
}

static void sendHello() {
  char b[64];
  snprintf(b, sizeof(b), "HELLO,%s,%lu,6", FW, (unsigned long)bootId);
  sendLine(b);
}

static void handleCommand(char *line) {
  // line = "PAYLOAD*HH"
  char *star = strrchr(line, '*');
  if (!star || strlen(star) != 3) { sendLine("NAK,?,FORMATO"); return; }
  *star = 0;
  char *end;
  unsigned long hh = strtoul(star + 1, &end, 16);
  if (*end != 0 || hh != cksum(line, strlen(line))) { sendLine("NAK,?,CHECKSUM"); return; }

  char b[96];
  if (!strcmp(line, "PING")) {
    snprintf(b, sizeof(b), "ACK,PING,%s,%lu", FW, (unsigned long)bootId);
    sendLine(b);
  } else if (!strncmp(line, "CFG,", 4)) {
    long mask, iv, os;
    if (running) { sendLine("NAK,CFG,OCUPADO"); return; }
    if (sscanf(line + 4, "%ld,%ld,%ld", &mask, &iv, &os) != 3) { sendLine("NAK,CFG,FORMATO"); return; }
    if (mask < 1 || mask > 63 || iv < 1000 || iv > 10000000L || os != 1) { sendLine("NAK,CFG,RANGO"); return; }
    channelMask = (uint8_t)mask; intervalUs = iv;
    snprintf(b, sizeof(b), "ACK,CFG,%ld,%ld,%ld", mask, iv, os);
    sendLine(b);
  } else if (!strcmp(line, "START")) {
    seq = 0; ovr = 0; txDrop = 0;
    running = true;
    sendLine("ACK,START");
    nextTick = micros() + intervalUs;
  } else if (!strcmp(line, "STOP")) {
    running = false;
    sendLine("ACK,STOP");
  } else if (!strcmp(line, "STATUS")) {
    snprintf(b, sizeof(b), "STATUS,%d,%lu,%lu,%lu", running ? 1 : 0, (unsigned long)seq, (unsigned long)ovr, (unsigned long)txDrop);
    sendLine(b);
  } else {
    snprintf(b, sizeof(b), "NAK,%.20s,DESCONOCIDO", line);
    sendLine(b);
  }
}

static void pollSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      cmdBuf[cmdLen] = 0;
      if (cmdLen > 0) handleCommand(cmdBuf);
      cmdLen = 0;
    } else if (c != '\r') {
      if (cmdLen < sizeof(cmdBuf) - 1) cmdBuf[cmdLen++] = c; else cmdLen = 0;  // linea demasiado larga: descartar
    }
  }
}

static void sampleAndSend() {
  char b[120];
  int len = snprintf(b, sizeof(b), "D,%lu,%lu,%lu,%u", (unsigned long)seq, (unsigned long)micros(), (unsigned long)ovr, channelMask);
  seq++;
  for (uint8_t i = 0; i < 6; i++) {
    if (!(channelMask & (1U << i))) continue;
    uint32_t raw = analogRead(PINS[i]);   // una sola lectura cruda, 12 bit
    len += snprintf(b + len, sizeof(b) - len, ",%u,%lu", i + 1, (unsigned long)raw);
  }
  sendLine(b);
}

void setup() {
  Serial.begin(BAUD);
  analogReadResolution(12);
  for (uint8_t i = 0; i < 6; i++) {
    pinMode(PINS[i], INPUT);
    analogSetPinAttenuation(PINS[i], ADC_6db);
  }
  bootId = esp_random();
  delay(50);
  sendHello();
}

void loop() {
  pollSerial();
  if (!running) return;
  int32_t late = (int32_t)(micros() - nextTick);
  if (late < 0) return;
  if ((uint32_t)late >= intervalUs) {       // nos atrasamos un ciclo completo o mas: contar y resincronizar
    ovr++;
    nextTick = micros();
  }
  nextTick += intervalUs;
  sampleAndSend();
}
