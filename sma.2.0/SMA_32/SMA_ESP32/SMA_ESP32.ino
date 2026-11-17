/*
  SPACE MEDICAL ASSISTANT (SMA)
  CREW MONITORING SYSTEM
  WEARABLE VERSION 2.3

  ESP32 Dev Module
  ESP32 Arduino Core 3.3.12
  USB Serial only

  REAL SENSOR:
    MPU-6050

  SIMULATED:
    Heart rate
    SpO2
    Temperature
    Radiation

  PINS:
    MPU SDA       GPIO21
    MPU SCL       GPIO22
    MPU VCC       3V3
    MPU GND       GND

    LED           GPIO4
    SPEAKER       GPIO25
    BUTTON        GPIO15
    VIBRATION     GPIO26

  IMPORTANT:
    4/8 ohm speaker requires an amplifier or transistor driver.
    Do not connect it directly to GPIO25.

    VIBRATION MOTOR must NOT be connected directly to GPIO26
    (GPIO gives ~12-20 mA, a motor needs 60-100 mA).
    Use an NPN transistor (2N2222 / S8050 / BC337):
      GPIO26 -> 1 kOhm -> transistor base
      transistor emitter -> GND
      motor (+) -> 3V3 (or 5V/VIN), motor (-) -> transistor collector
      diode 1N4148 / 1N4007 across the motor (stripe to motor +)
    Or use a ready module with a driver (e.g. "vibration motor module").
    Serial command VIBTEST turns it on for 2 s.
*/

#include <Arduino.h>
#include <Wire.h>
#include <esp_system.h>
#include <string.h>
#include <ctype.h>
#include <math.h>

// ==========================================
// CONFIGURATION
// ==========================================

#define ENABLE_SPEAKER true
#define ENABLE_MPU true

// ==========================================
// PINS
// ==========================================

const int LED_PIN = 4;
const int SPEAKER_PIN = 25;
const int BUTTON_PIN = 15;
const int VIB_PIN = 26;

const int SDA_PIN = 21;
const int SCL_PIN = 22;

const uint8_t MPU_ADDR = 0x68;

// ==========================================
// DEVICE ID
// ==========================================

const char* ASTRONAUT_ID = "AST-001";
const char* DEVICE_ID = "WATCH-001";
const char* HR_SOURCE = "sim";

// ==========================================
// MEASUREMENTS
// ==========================================

char missionPhase[16] = "orbit";

int heartRate = 72;
int spo2 = 98;

float temperature = 36.6f;
float radiation = 20.0f;

double doseTotal = 0.0;

char motionState[16] = "rest";

bool mpuOk = false;

float lastG = 1.0f;
float motionAvg = 0.0f;

unsigned long freeFallTime = 0;
unsigned long fallShownUntil = 0;

int lowCount = 0;
int zeroCount = 0;
int mpuFailCount = 0;

// ==========================================
// ALARMS
// ==========================================

bool localAlarm = false;
bool serverAlarm = false;

// ==========================================
// SPEAKER
// ==========================================

bool speakerOk = false;

int lastSpeakerState = -1;

const uint32_t SPEAKER_FREQ = 2000;
const uint8_t SPEAKER_RESOLUTION = 8;

const uint32_t SPEAKER_ON_DUTY = 128;
const uint32_t SPEAKER_OFF_DUTY = 0;

// ==========================================
// SPEAKER TEST
// ==========================================

bool speakerTestActive = false;
unsigned long speakerTestUntil = 0;

// ==========================================
// VIBRATION
// ==========================================

const unsigned long NOTIFY_VIB_MS = 2000;

bool vibActive = false;
unsigned long vibUntil = 0;

// ==========================================
// SERIAL / TIMERS
// ==========================================

const unsigned long LINK_TIMEOUT = 10000;

unsigned long lastPing = 0;
unsigned long lastCycle = 0;
unsigned long lastMotionRead = 0;
unsigned long lastLinkWarning = 0;

char cmdBuf[101];
int cmdLen = 0;

char jsonLine[360];

// ==========================================
// READING BUFFER
// ==========================================

struct Reading {
  unsigned long t;
  int hr;
  int spo2;
  float temp;
  char motion[16];
  float rad;
  double dose;
  char phase[16];
  bool manual;
};

const int BUF_SIZE = 30;

Reading readings[BUF_SIZE];

int bufCount = 0;
int bufHead = 0;

// ==========================================
// SAFE BUFFER INDEX
// No modulo operation needed
// ==========================================

int nextBufferIndex(int index) {
  index++;
  if (index >= BUF_SIZE) {
    index = 0;
  }
  return index;
}

// ==========================================
// SPEAKER INITIALIZATION
// ESP32 CORE 3.x
// ==========================================

bool speakerInit() {

#if ENABLE_SPEAKER

  Serial.println("SPEAKER: initializing GPIO25...");

  speakerOk = ledcAttach(
    SPEAKER_PIN,
    SPEAKER_FREQ,
    SPEAKER_RESOLUTION
  );

  if (!speakerOk) {

    Serial.println(
      "ERROR: LEDC ATTACH FAILED ON GPIO25"
    );

    return false;
  }

  if (!ledcWrite(SPEAKER_PIN, SPEAKER_OFF_DUTY)) {

    Serial.println(
      "ERROR: INITIAL SPEAKER PWM WRITE FAILED"
    );

    ledcDetach(SPEAKER_PIN);

    speakerOk = false;

    return false;
  }

  lastSpeakerState = 0;

  Serial.println("SPEAKER: PWM READY");

  return true;

#else

  speakerOk = false;

  Serial.println("SPEAKER: DISABLED");

  return false;

#endif
}

// ==========================================
// SPEAKER CONTROL
// ==========================================

void speakerTone(bool enabled) {

#if ENABLE_SPEAKER

  if (!speakerOk) {
    return;
  }

  int state = enabled ? 1 : 0;

  if (state == lastSpeakerState) {
    return;
  }

  uint32_t duty = enabled
    ? SPEAKER_ON_DUTY
    : SPEAKER_OFF_DUTY;

  if (!ledcWrite(SPEAKER_PIN, duty)) {

    Serial.println(
      "ERROR: SPEAKER PWM WRITE FAILED"
    );

    speakerOk = false;

    lastSpeakerState = -1;

    return;
  }

  lastSpeakerState = state;

#endif
}

// ==========================================
// LINK STATUS
// ==========================================

bool isLinkUp() {

  if (lastPing == 0) {
    return false;
  }

  return (unsigned long)(millis() - lastPing)
         < LINK_TIMEOUT;
}

const char* statusText() {

  if (localAlarm || (serverAlarm && isLinkUp())) {
    return "WARNING";
  }

  if (isLinkUp()) {
    return "CONNECTED";
  }

  return "CONNECTION_LOST";
}

// ==========================================
// READING MANAGEMENT
// ==========================================

void fillReading(Reading &r) {

  r.t = millis();

  r.hr = heartRate;
  r.spo2 = spo2;
  r.temp = temperature;

  snprintf(
    r.motion,
    sizeof(r.motion),
    "%s",
    motionState
  );

  r.rad = radiation;
  r.dose = doseTotal;

  snprintf(
    r.phase,
    sizeof(r.phase),
    "%s",
    missionPhase
  );

  r.manual = localAlarm;
}

// ==========================================
// JSON
// ==========================================

void makeJson(
  const Reading &r,
  bool buffered,
  char *out,
  size_t n
) {

  snprintf(
    out,
    n,

    "{\"astronaut_id\":\"%s\","
    "\"device_id\":\"%s\","
    "\"timestamp\":%lu,"
    "\"heart_rate\":%d,"
    "\"heart_rate_source\":\"%s\","
    "\"spo2\":%d,"
    "\"temperature\":%.1f,"
    "\"motion\":\"%s\","
    "\"radiation_usv_h\":%.1f,"
    "\"dose_total_msv\":%.5f,"
    "\"mission_phase\":\"%s\","
    "\"manual_alarm\":%s,"
    "\"buffered\":%s}",

    ASTRONAUT_ID,
    DEVICE_ID,
    r.t,
    r.hr,
    HR_SOURCE,
    r.spo2,
    r.temp,
    r.motion,
    r.rad,
    r.dose,
    r.phase,
    r.manual ? "true" : "false",
    buffered ? "true" : "false"
  );
}

// ==========================================
// BUFFER PUSH
// ==========================================

void bufPush(const Reading &r) {

  int idx = bufHead + bufCount;

  if (idx >= BUF_SIZE) {
    idx -= BUF_SIZE;
  }

  readings[idx] = r;

  if (bufCount < BUF_SIZE) {

    bufCount++;

  } else {

    bufHead = nextBufferIndex(bufHead);
  }
}

// ==========================================
// SEND JSON
// ==========================================

bool transmit(const char *json) {

  if (!isLinkUp()) {
    return false;
  }

  Serial.println(json);

  return true;
}

void sendCycle() {

  Reading current;

  fillReading(current);

  int flushed = 0;

  // Send old readings first

  while (bufCount > 0 && flushed < 5) {

    if (!isLinkUp()) {
      break;
    }

    makeJson(
      readings[bufHead],
      true,
      jsonLine,
      sizeof(jsonLine)
    );

    if (!transmit(jsonLine)) {
      break;
    }

    bufHead = nextBufferIndex(bufHead);

    bufCount--;

    flushed++;
  }

  // Send current reading

  if (bufCount == 0) {

    makeJson(
      current,
      false,
      jsonLine,
      sizeof(jsonLine)
    );

    if (transmit(jsonLine)) {
      return;
    }
  }

  // Save when disconnected

  bufPush(current);

  if (
    (unsigned long)(millis() - lastLinkWarning)
    >= 5000
  ) {

    lastLinkWarning = millis();

    Serial.print("SMA: LINK DOWN, BUFFERED: ");

    Serial.println(bufCount);
  }
}

// ==========================================
// MPU INITIALIZATION
// ==========================================

void mpuInit() {

#if ENABLE_MPU

  Serial.println("MPU: initializing I2C...");

  Wire.begin(SDA_PIN, SCL_PIN);

  Wire.setClock(100000);

  Wire.setTimeOut(50);

  // Wake sensor

  Wire.beginTransmission(MPU_ADDR);

  Wire.write(0x6B);

  Wire.write(0x00);

  bool wakeOk = (
    Wire.endTransmission(true) == 0
  );

  mpuOk = false;

  if (!wakeOk) {

    Serial.println("MPU: WAKE FAILED");

    strcpy(motionState, "no_sensor");

    return;
  }

  delay(50);

  // Read WHO_AM_I

  Wire.beginTransmission(MPU_ADDR);

  Wire.write(0x75);

  if (Wire.endTransmission(false) == 0) {

    uint8_t got = Wire.requestFrom(
      MPU_ADDR,
      (uint8_t)1,
      (uint8_t)1
    );

    if (got == 1) {

      int who = Wire.read();

      Serial.print("MPU WHO_AM_I: 0x");

      Serial.println(who, HEX);

      mpuOk = (
        who == 0x68 ||
        who == 0x70 ||
        who == 0x71 ||
        who == 0x72
      );
    }
  }

  if (mpuOk) {

    Serial.println("MPU: READY");

  } else {

    Serial.println("MPU: NOT FOUND");

    strcpy(motionState, "no_sensor");
  }

#else

  mpuOk = false;

  strcpy(motionState, "no_sensor");

#endif
}

// ==========================================
// MPU READ
// ==========================================

bool mpuRead(float &g) {

  Wire.beginTransmission(MPU_ADDR);

  Wire.write(0x3B);

  if (Wire.endTransmission(false) != 0) {
    return false;
  }

  uint8_t got = Wire.requestFrom(
    MPU_ADDR,
    (uint8_t)6,
    (uint8_t)1
  );

  if (got != 6) {
    return false;
  }

  uint8_t b[6];

  for (int i = 0; i < 6; i++) {

    int v = Wire.read();

    if (v < 0) {
      return false;
    }

    b[i] = (uint8_t)v;
  }

  int16_t ax = (int16_t)(
    ((uint16_t)b[0] << 8) | b[1]
  );

  int16_t ay = (int16_t)(
    ((uint16_t)b[2] << 8) | b[3]
  );

  int16_t az = (int16_t)(
    ((uint16_t)b[4] << 8) | b[5]
  );

  float x = (float)ax / 16384.0f;
  float y = (float)ay / 16384.0f;
  float z = (float)az / 16384.0f;

  g = sqrtf(
    x * x +
    y * y +
    z * z
  );

  return true;
}

// ==========================================
// MOTION UPDATE
// ==========================================

void updateMotion() {

  unsigned long now = millis();

  if (!mpuOk) {

    strcpy(motionState, "no_sensor");

    return;
  }

  float g = 1.0f;

  bool readOk = mpuRead(g);

  if (!readOk) {

    mpuFailCount++;

    if (mpuFailCount >= 50) {

      mpuOk = false;

      strcpy(motionState, "no_sensor");

      Serial.println(
        "MPU: SENSOR STOPPED ANSWERING"
      );
    }

    return;
  }

  mpuFailCount = 0;

  lastG = g;

  // Sensor connection check

  if (g < 0.1f) {

    if (zeroCount < 1000) {
      zeroCount++;
    }

  } else {

    zeroCount = 0;
  }

  if (zeroCount >= 100) {

    strcpy(motionState, "no_sensor");

    Serial.println(
      "MPU: CHECK SENSOR WIRING"
    );

    mpuOk = false;

    return;
  }

  // Free fall detection

  if (g < 0.4f) {

    lowCount++;

    freeFallTime = now;
  }

  if (
    freeFallTime != 0 &&
    (unsigned long)(now - freeFallTime) > 500
  ) {

    freeFallTime = 0;

    lowCount = 0;
  }

  // Impact detection

  if (
    g > 2.5f &&
    lowCount >= 3 &&
    freeFallTime != 0 &&
    now > 3000
  ) {

    fallShownUntil = now + 5000;

    freeFallTime = 0;

    lowCount = 0;

    localAlarm = true;

    Serial.println("SMA: FALL DETECTED");
  }

  float dev = fabsf(g - 1.0f);

  motionAvg = (
    0.9f * motionAvg +
    0.1f * dev
  );

  if (
    fallShownUntil != 0 &&
    (long)(fallShownUntil - now) > 0
  ) {

    strcpy(motionState, "fall");

  } else if (motionAvg > 0.15f) {

    strcpy(motionState, "moving");

  } else {

    strcpy(motionState, "rest");
  }
}

// ==========================================
// SIMULATED VALUES
// ==========================================

float phaseBase() {

  if (!strcmp(missionPhase, "launch")) {
    return 2.0f;
  }

  if (!strcmp(missionPhase, "deep_space")) {
    return 60.0f;
  }

  if (!strcmp(missionPhase, "landing")) {
    return 3.0f;
  }

  return 20.0f;
}

void updateSimulated() {

  int base = (
    !strcmp(motionState, "moving")
  ) ? 92 : 72;

  heartRate = base + random(-3, 4);

  spo2 = 97 + random(0, 3);

  temperature =
    36.6f + (float)random(-2, 3) / 10.0f;

  radiation =
    phaseBase() *
    (1.0f + (float)random(-10, 11) / 100.0f);

  doseTotal += (
    (double)radiation / 3600.0 / 1000.0
  );
}

// ==========================================
// VIBRATION
// ==========================================

void startVibration(unsigned long duration) {

  vibActive = true;

  vibUntil = millis() + duration;

  digitalWrite(VIB_PIN, HIGH);
}

void updateVibration() {

  unsigned long now = millis();

  bool alarm = (
    localAlarm ||
    (serverAlarm && isLinkUp())
  );

  // During an alarm the motor pulses: 300 ms on, 500 ms off.
  if (alarm) {

    vibActive = false;

    digitalWrite(
      VIB_PIN,
      ((now % 800) < 300) ? HIGH : LOW
    );

    return;
  }

  // Timed pulse (NOTIFY / VIBTEST commands).
  if (
    vibActive &&
    (long)(now - vibUntil) >= 0
  ) {

    vibActive = false;
  }

  digitalWrite(VIB_PIN, vibActive ? HIGH : LOW);
}

// ==========================================
// SPEAKER TEST
// ==========================================

void startSpeakerTest() {

  if (!speakerOk) {

    Serial.println(
      "ERROR: SPEAKER NOT INITIALIZED"
    );

    return;
  }

  speakerTestActive = true;

  speakerTestUntil = millis() + 2000;

  Serial.println("SMA: SPEAKER TEST STARTED");
}

// ==========================================
// LED AND SPEAKER INDICATORS
// ==========================================

void updateIndicators() {

  unsigned long now = millis();

  bool alarm = (
    localAlarm ||
    (serverAlarm && isLinkUp())
  );

  bool testActive = (
    speakerTestActive &&
    (long)(speakerTestUntil - now) > 0
  );

  if (
    speakerTestActive &&
    !testActive
  ) {

    speakerTestActive = false;
  }

  bool soundOn = false;

  if (alarm) {

    bool blink = ((now / 150) & 1) != 0;

    digitalWrite(LED_PIN, blink ? HIGH : LOW);

    soundOn = blink;

  } else {

    if (isLinkUp()) {

      digitalWrite(LED_PIN, HIGH);

    } else {

      bool blink = ((now / 1000) & 1) != 0;

      digitalWrite(LED_PIN, blink ? HIGH : LOW);
    }

    if (testActive) {
      soundOn = true;
    }
  }

  speakerTone(soundOn);
}

// ==========================================
// BUTTON
// ==========================================

void checkButton() {

  static bool initialized = false;

  static bool lastRead = HIGH;
  static bool stable = HIGH;

  static unsigned long changed = 0;

  if (!initialized) {

    lastRead = digitalRead(BUTTON_PIN);

    stable = lastRead;

    initialized = true;
  }

  bool r = digitalRead(BUTTON_PIN);

  if (r != lastRead) {

    changed = millis();

    lastRead = r;
  }

  if (
    (unsigned long)(millis() - changed) > 50 &&
    r != stable
  ) {

    stable = r;

    if (stable == LOW) {

      localAlarm = !localAlarm;

      if (!localAlarm) {
        fallShownUntil = 0;
      }

      Serial.println(
        localAlarm
          ? "SMA: MANUAL ALARM ON"
          : "SMA: MANUAL ALARM OFF"
      );
    }
  }
}

// ==========================================
// COMMAND NORMALIZATION
// ==========================================

void normalizeCommand(char *t) {

  char *a = t;

  while (*a == ' ' || *a == '\t') {
    a++;
  }

  if (a != t) {
    memmove(t, a, strlen(a) + 1);
  }

  size_t n = strlen(t);

  while (
    n > 0 &&
    (t[n - 1] == ' ' || t[n - 1] == '\t')
  ) {

    t[--n] = '\0';
  }

  for (char *q = t; *q; q++) {

    *q = (char)toupper((unsigned char)*q);
  }
}

// ==========================================
// STATUS COMMAND
// ==========================================

void printStatus() {

  Serial.println();
  Serial.println("========== SMA STATUS ==========");

  Serial.print("DEVICE: ");
  Serial.println(DEVICE_ID);

  Serial.print("LINK: ");
  Serial.println(statusText());

  Serial.print("MPU: ");
  Serial.println(mpuOk ? "OK" : "OFF");

  Serial.print("SPEAKER: ");
  Serial.println(speakerOk ? "ON" : "OFF");

  Serial.print("BUTTON: ");
  Serial.println(
    digitalRead(BUTTON_PIN) == LOW
      ? "PRESSED"
      : "released"
  );

  Serial.print("LED: ");
  Serial.println(digitalRead(LED_PIN) ? "ON" : "OFF");

  Serial.print("LOCAL ALARM: ");
  Serial.println(localAlarm ? "ON" : "OFF");

  Serial.print("SERVER ALARM: ");
  Serial.println(serverAlarm ? "ON" : "OFF");

  Serial.print("MOTION: ");
  Serial.println(motionState);

  Serial.print("ACCEL: ");
  Serial.print(lastG, 2);
  Serial.println(" g");

  Serial.print("BUFFER: ");
  Serial.println(bufCount);

  Serial.println("================================");
}

// ==========================================
// COMMAND PROCESSOR
// ==========================================

void processCommand(char *command) {

  normalizeCommand(command);

  if (command[0] == '\0') {
    return;
  }

  lastPing = millis();

  if (!strcmp(command, "PING")) {

    Serial.println("SMA: PONG");
  }

  else if (!strcmp(command, "STATUS")) {

    printStatus();
  }

  else if (!strcmp(command, "HELP")) {

    Serial.println("SMA COMMANDS:");
    Serial.println("PING");
    Serial.println("STATUS");
    Serial.println("VITALS");
    Serial.println("MPU");
    Serial.println("ALERT");
    Serial.println("SAFE");
    Serial.println("BEEP");
    Serial.println("NOTIFY");
    Serial.println("VIBTEST");
    Serial.println("ALARM 1");
    Serial.println("ALARM 0");
    Serial.println("PHASE LAUNCH");
    Serial.println("PHASE ORBIT");
    Serial.println("PHASE DEEP_SPACE");
    Serial.println("PHASE LANDING");
  }

  else if (!strcmp(command, "BEEP")) {

    startSpeakerTest();
  }

  else if (!strcmp(command, "ALERT")) {

    localAlarm = true;

    Serial.println("SMA: MANUAL ALARM ON");
  }

  else if (!strcmp(command, "SAFE")) {

    localAlarm = false;

    fallShownUntil = 0;

    speakerTestActive = false;

    Serial.println("SMA: ALARM RESET");
  }

  else if (!strcmp(command, "ALARM 1")) {

    serverAlarm = true;

    Serial.println("SMA: SERVER ALARM ON");
  }

  else if (!strcmp(command, "ALARM 0")) {

    serverAlarm = false;

    Serial.println("SMA: SERVER ALARM OFF");
  }

  else if (!strcmp(command, "NOTIFY")) {

    startVibration(NOTIFY_VIB_MS);

    Serial.println("SMA: NOTIFICATION");
  }

  else if (!strcmp(command, "VIBTEST")) {

    startVibration(NOTIFY_VIB_MS);

    Serial.println("SMA: VIBRATION TEST (GPIO26 HIGH for 2 s)");
  }

  else if (!strcmp(command, "VITALS")) {

    Serial.print("HR: ");
    Serial.print(heartRate);

    Serial.print(" | SpO2: ");
    Serial.print(spo2);

    Serial.print(" | TEMP: ");
    Serial.print(temperature, 1);

    Serial.print(" | RAD: ");
    Serial.print(radiation, 1);

    Serial.print(" uSv/h | DOSE: ");
    Serial.print(doseTotal, 5);

    Serial.println(" mSv (SIMULATED)");
  }

  else if (!strcmp(command, "MPU")) {

    Serial.print("MOTION: ");
    Serial.println(motionState);

    Serial.print("ACCEL: ");
    Serial.print(lastG, 2);
    Serial.println(" g");
  }

  else if (!strncmp(command, "PHASE ", 6)) {

    const char *p = command + 6;

    if (
      !strcmp(p, "LAUNCH") ||
      !strcmp(p, "ORBIT") ||
      !strcmp(p, "DEEP_SPACE") ||
      !strcmp(p, "LANDING")
    ) {

      size_t i = 0;

      while (
        p[i] &&
        i < sizeof(missionPhase) - 1
      ) {

        missionPhase[i] =
          (char)tolower((unsigned char)p[i]);

        i++;
      }

      missionPhase[i] = '\0';

      Serial.print("SMA: PHASE ");
      Serial.println(missionPhase);

    } else {

      Serial.println(
        "ERROR: PHASES: LAUNCH, ORBIT, DEEP_SPACE, LANDING"
      );
    }
  }

  else {

    Serial.print("UNKNOWN COMMAND: ");
    Serial.println(command);
    Serial.println("SEND HELP FOR COMMAND LIST");
  }
}

// ==========================================
// SERIAL INPUT
// ==========================================

void handleSerial() {

  while (Serial.available() > 0) {

    char c = (char)Serial.read();

    if (c == '\n' || c == '\r') {

      if (cmdLen > 0) {

        cmdBuf[cmdLen] = '\0';

        processCommand(cmdBuf);

        cmdLen = 0;
      }

    } else {

      if (cmdLen < (int)sizeof(cmdBuf) - 1) {

        cmdBuf[cmdLen++] = c;

      } else {

        cmdLen = 0;

        Serial.println(
          "ERROR: SERIAL COMMAND TOO LONG"
        );
      }
    }
  }
}

// ==========================================
// RESET REASON
// ==========================================

const char* resetReasonText(esp_reset_reason_t r) {

  switch (r) {

    case ESP_RST_POWERON:
      return "POWER ON";

    case ESP_RST_SW:
      return "SOFTWARE RESET";

    case ESP_RST_PANIC:
      return "PANIC / CRASH";

    case ESP_RST_INT_WDT:
      return "INTERRUPT WATCHDOG";

    case ESP_RST_TASK_WDT:
      return "TASK WATCHDOG";

    case ESP_RST_WDT:
      return "WATCHDOG";

    case ESP_RST_BROWNOUT:
      return "BROWNOUT / POWER DIP";

    default:
      return "OTHER";
  }
}

// ==========================================
// SETUP
// ==========================================

void setup() {

  Serial.begin(115200);

  delay(500);

  Serial.println();
  Serial.println("================================");
  Serial.println(" SPACE MEDICAL ASSISTANT");
  Serial.println(" SMA WEARABLE VERSION 2.3");
  Serial.println(" ESP32 CORE 3.x");
  Serial.println("================================");

  Serial.print("RESET REASON: ");

  Serial.println(
    resetReasonText(esp_reset_reason())
  );

  randomSeed(esp_random());

  // GPIO

  pinMode(LED_PIN, OUTPUT);

  pinMode(VIB_PIN, OUTPUT);

  pinMode(BUTTON_PIN, INPUT_PULLUP);

  digitalWrite(LED_PIN, LOW);

  digitalWrite(VIB_PIN, LOW);

  Serial.println("BOOT: GPIO READY");

  // Speaker

  Serial.println("BOOT: SPEAKER INIT");

  speakerInit();

  // MPU

  Serial.println("BOOT: MPU INIT");

  mpuInit();

  // Timers

  lastCycle = millis();

  lastMotionRead = millis();

  Serial.println();
  Serial.println("================================");
  Serial.println(" SMA SYSTEM INITIALIZED");

  Serial.print("DEVICE: ");
  Serial.println(DEVICE_ID);

  Serial.print("SPEAKER: ");
  Serial.println(speakerOk ? "ON" : "OFF");

  Serial.print("MPU: ");
  Serial.println(mpuOk ? "OK" : "NOT FOUND");

  Serial.println("USB SERIAL: 115200");

  Serial.println("SEND HELP FOR COMMANDS");

  Serial.println("================================");
}

// ==========================================
// MAIN LOOP
// ==========================================

void loop() {

  unsigned long now = millis();

  // Serial commands

  handleSerial();

  // Physical button

  checkButton();

  // MPU read every 20 ms

  if (
    (unsigned long)(now - lastMotionRead) >= 20
  ) {

    lastMotionRead = now;

    updateMotion();
  }

  // Measurements every 1 second

  if (
    (unsigned long)(now - lastCycle) >= 1000
  ) {

    lastCycle = now;

    updateSimulated();

    sendCycle();
  }

  // Indicators

  updateIndicators();

  // Vibration

  updateVibration();
}