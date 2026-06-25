/*
  ALPS motorized fader + L9110 driver (hybrid v5)
  - GUI sets target -> motor moves to target
  - Manual movement -> GUI follows position when not auto-moving
  Pins:
    - Fader wiper -> A0
    - L9110 IA    -> D5
    - L9110 IB    -> D6
*/

const uint8_t FADER_PIN = A0;
const uint8_t MOTOR_IA_PIN = 5;
const uint8_t MOTOR_IB_PIN = 6;
const uint8_t TOUCH_PIN = 7;

// Flip this if movement direction is reversed.
const bool MOTOR_FORWARD_INCREASES_ADC = true;

// Control tuning.
const uint8_t MOTOR_MIN_PWM = 150;
const uint8_t MOTOR_MAX_PWM = 255;
const float KP = 0.38f;
const int POS_DEADBAND = 6;
const int MANUAL_TAKEOVER_DELTA = 20;
const int MANUAL_MOVE_DELTA = 3;
const int MANUAL_TARGET_SYNC_DEADBAND = 6;
const unsigned long COMMAND_GRACE_MS = 180;
const unsigned long CONTROL_INTERVAL_US = 2500;  // 400 Hz control loop
const float TARGET_SMOOTH_ALPHA = 0.30f;         // command setpoint smoothing
const uint8_t PWM_RAMP_STEP = 10;                // smooth PWM transitions
const float FILTER_ALPHA = 0.22f;
const uint8_t ADC_AVG_SAMPLES = 2;
const uint8_t ADC_STABLE_DEADBAND = 2;
const unsigned long REPORT_MIN_INTERVAL_MS = 20;
const int REPORT_POS_DELTA_IDLE = 10;
const int REPORT_POS_DELTA_ACTIVE = 2;
const int REPORT_TARGET_DELTA = 3;
const bool USE_TOUCH = true;
const int TOUCH_ON_DELTA = 22;
const int TOUCH_OFF_DELTA = 10;
const uint8_t TOUCH_ON_COUNT = 3;
const uint8_t TOUCH_OFF_COUNT = 4;
const unsigned long TOUCH_RELEASE_HOLD_MS = 220;

int targetPosition = 512;
float smoothedTarget = 512.0f;
float filteredPosition = 512.0f;
int stablePosition = 512;
int lastPosition = 512;
bool autoMode = false;
int touchBaseline = 0;
bool touchState = false;
uint8_t touchOnCounter = 0;
uint8_t touchOffCounter = 0;
unsigned long touchHoldUntilMs = 0;
unsigned long lastCommandMs = 0;
unsigned long lastControlUs = 0;
unsigned long lastReportMs = 0;
int lastReportPos = -1;
int lastReportTarget = -1;
int lastReportMoving = -1;
int lastReportTouch = -1;
bool reportPending = true;
int appliedPwm = 0;
int appliedDirection = 0;  // -1 toward lower, +1 toward higher, 0 stopped

String serialLine;

void configurePwmNear1kHzOnD5D6() {
#if defined(TCCR0A) && defined(TCCR0B)
  // D5/D6 are on Timer0. Closest hardware rate to 1 kHz on Uno is 976.56 Hz:
  // F_CPU / (prescaler 64 * 256) = 16 MHz / 16384.
  // Keep Fast PWM mode and prescaler at 64.
  TCCR0A |= _BV(WGM00) | _BV(WGM01);
  TCCR0B = (TCCR0B & 0b11111000) | 0x03;
#endif
}

int readAveragedAdc() {
  long sum = 0;
  for (uint8_t i = 0; i < ADC_AVG_SAMPLES; ++i) {
    sum += analogRead(FADER_PIN);
  }
  return (int)(sum / ADC_AVG_SAMPLES);
}

int readPosition() {
  int raw = readAveragedAdc();
  filteredPosition += FILTER_ALPHA * (raw - filteredPosition);
  int candidate = (int)(filteredPosition + 0.5f);
  if (abs(candidate - stablePosition) >= ADC_STABLE_DEADBAND) {
    stablePosition = candidate;
  }
  return stablePosition;
}

int readTouchCycles() {
  // For a 1M pull-up on TOUCH_PIN:
  // 1) discharge node to GND
  // 2) switch to input
  // 3) measure time until external pull-up reads HIGH
  pinMode(TOUCH_PIN, OUTPUT);
  digitalWrite(TOUCH_PIN, LOW);
  delayMicroseconds(6);
  pinMode(TOUCH_PIN, INPUT);

  int cycles = 0;
  while (digitalRead(TOUCH_PIN) == LOW && cycles < 4000) {
    cycles++;
  }
  return cycles;
}

void calibrateTouch() {
  if (!USE_TOUCH) return;
  long sum = 0;
  int validCount = 0;
  for (int i = 0; i < 24; i++) {
    int v = readTouchCycles();
    // Ignore saturated samples to avoid bad baseline = 4000.
    if (v < 3999) {
      sum += v;
      validCount++;
    }
    delay(2);
  }
  if (validCount >= 8) {
    touchBaseline = (int)(sum / validCount);
  } else {
    // Fallback baseline if wiring/sample quality is poor.
    touchBaseline = 200;
  }
  touchState = false;
  touchOnCounter = 0;
  touchOffCounter = 0;
}

bool updateTouchState() {
  if (!USE_TOUCH) return false;
  int v = readTouchCycles();
  bool wantOn = v > (touchBaseline + TOUCH_ON_DELTA);
  bool wantOff = v < (touchBaseline + TOUCH_OFF_DELTA);

  if (!touchState) {
    if (wantOn) {
      if (touchOnCounter < 255) touchOnCounter++;
      touchOffCounter = 0;
      if (touchOnCounter >= TOUCH_ON_COUNT) {
        touchState = true;
        touchOnCounter = 0;
      }
    } else {
      touchOnCounter = 0;
    }
  } else {
    if (wantOff) {
      if (touchOffCounter < 255) touchOffCounter++;
      touchOnCounter = 0;
      if (touchOffCounter >= TOUCH_OFF_COUNT) {
        touchState = false;
        touchOffCounter = 0;
      }
    } else {
      touchOffCounter = 0;
    }
  }

  return touchState;
}

void reportStatus(int currentPosition, bool force = false) {
  int moving = autoMode ? 1 : 0;
  int touch = touchState ? 1 : 0;
  int posDeltaNeeded = (moving || touch) ? REPORT_POS_DELTA_ACTIVE : REPORT_POS_DELTA_IDLE;
  bool changed =
      force || reportPending || lastReportPos < 0 ||
      abs(currentPosition - lastReportPos) >= posDeltaNeeded ||
      abs(targetPosition - lastReportTarget) >= REPORT_TARGET_DELTA ||
      moving != lastReportMoving ||
      touch != lastReportTouch;

  if (!changed) return;

  unsigned long now = millis();
  if (!force && (now - lastReportMs) < REPORT_MIN_INTERVAL_MS) {
    reportPending = true;
    return;
  }

  Serial.print(F("pos="));
  Serial.print(currentPosition);
  Serial.print(F(" target="));
  Serial.print(targetPosition);
  Serial.print(F(" moving="));
  Serial.print(moving);
  Serial.print(F(" touch="));
  Serial.println(touch);

  lastReportPos = currentPosition;
  lastReportTarget = targetPosition;
  lastReportMoving = moving;
  lastReportTouch = touch;
  lastReportMs = now;
  reportPending = false;
}

void motorStop() {
  analogWrite(MOTOR_IA_PIN, 0);
  analogWrite(MOTOR_IB_PIN, 0);
  digitalWrite(MOTOR_IA_PIN, LOW);
  digitalWrite(MOTOR_IB_PIN, LOW);
}

void motorDriveTowardHigher(uint8_t pwm) {
  if (MOTOR_FORWARD_INCREASES_ADC) {
    analogWrite(MOTOR_IA_PIN, pwm);
    analogWrite(MOTOR_IB_PIN, 0);
  } else {
    analogWrite(MOTOR_IA_PIN, 0);
    analogWrite(MOTOR_IB_PIN, pwm);
  }
}

void motorDriveTowardLower(uint8_t pwm) {
  if (MOTOR_FORWARD_INCREASES_ADC) {
    analogWrite(MOTOR_IA_PIN, 0);
    analogWrite(MOTOR_IB_PIN, pwm);
  } else {
    analogWrite(MOTOR_IA_PIN, pwm);
    analogWrite(MOTOR_IB_PIN, 0);
  }
}

void driveMotorSmooth(int direction, int requestedPwm) {
  int targetPwm = (direction == 0) ? 0 : requestedPwm;

  // If direction flips, fully release first to avoid abrupt reversal.
  if (direction != 0 && appliedDirection != 0 && direction != appliedDirection) {
    motorStop();
    appliedPwm = 0;
    appliedDirection = 0;
  }

  if (targetPwm > appliedPwm) {
    appliedPwm = min(targetPwm, appliedPwm + PWM_RAMP_STEP);
  } else if (targetPwm < appliedPwm) {
    appliedPwm = max(targetPwm, appliedPwm - PWM_RAMP_STEP);
  }

  if (appliedPwm <= 0 || direction == 0) {
    motorStop();
    appliedPwm = 0;
    appliedDirection = 0;
    return;
  }

  appliedDirection = direction;
  if (direction > 0) {
    motorDriveTowardHigher((uint8_t)appliedPwm);
  } else {
    motorDriveTowardLower((uint8_t)appliedPwm);
  }
}

void setTargetFromCommand(int value) {
  targetPosition = constrain(value, 0, 1023);
  autoMode = true;
  lastCommandMs = millis();
  Serial.print(F("target="));
  Serial.println(targetPosition);
  reportPending = true;
}

void printHelp() {
  Serial.println(F("FW:HYBRID_V6_SMOOTH"));
  Serial.println(F("Commands: 0..1023, min, center, max, stop, read, cal, ?"));
}

bool parseNumber(const String &s, int &out) {
  if (s.length() == 0) return false;
  for (size_t i = 0; i < s.length(); i++) {
    char c = s.charAt(i);
    if (c < '0' || c > '9') return false;
  }
  long value = s.toInt();
  if (value < 0 || value > 1023) return false;
  out = (int)value;
  return true;
}

void handleLine(const String &line, int currentPosition) {
  if (line == "?") {
    printHelp();
    return;
  }
  if (line == "min") {
    setTargetFromCommand(0);
    return;
  }
  if (line == "center") {
    setTargetFromCommand(512);
    return;
  }
  if (line == "max") {
    setTargetFromCommand(1023);
    return;
  }
  if (line == "stop") {
    autoMode = false;
    targetPosition = currentPosition;
    smoothedTarget = (float)currentPosition;
    driveMotorSmooth(0, 0);
    Serial.println(F("stopped"));
    reportPending = true;
    return;
  }
  if (line == "read") {
    reportStatus(currentPosition, true);
    return;
  }
  if (line == "cal") {
    calibrateTouch();
    Serial.print(F("touch_base="));
    Serial.println(touchBaseline);
    reportPending = true;
    return;
  }

  int parsed = 0;
  if (parseNumber(line, parsed)) {
    setTargetFromCommand(parsed);
    return;
  }

  Serial.println(F("Unknown command"));
}

void handleSerial(int currentPosition) {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      if (serialLine.length() > 0) {
        handleLine(serialLine, currentPosition);
        serialLine = "";
      }
    } else {
      serialLine += c;
    }
  }
}

void setup() {
  configurePwmNear1kHzOnD5D6();
  pinMode(MOTOR_IA_PIN, OUTPUT);
  pinMode(MOTOR_IB_PIN, OUTPUT);
  pinMode(TOUCH_PIN, INPUT);
  motorStop();

  Serial.begin(115200);
  delay(100);

  int initial = readAveragedAdc();
  filteredPosition = (float)initial;
  stablePosition = initial;
  lastPosition = initial;
  targetPosition = initial;
  smoothedTarget = (float)initial;
  calibrateTouch();

  printHelp();
  reportStatus(initial, true);
}

void loop() {
  int currentPosition = readPosition();
  int positionDelta = currentPosition - lastPosition;
  lastPosition = currentPosition;
  handleSerial(currentPosition);

  bool touched = updateTouchState();
  if (touched) {
    touchHoldUntilMs = millis() + TOUCH_RELEASE_HOLD_MS;
  }
  bool touchActive = touchState || (millis() < touchHoldUntilMs);

  if (touchActive) {
    // Touch always wins: release motor and follow the hand.
    autoMode = false;
    if (abs(targetPosition - currentPosition) >= MANUAL_TARGET_SYNC_DEADBAND) {
      targetPosition = currentPosition;
      smoothedTarget = (float)currentPosition;
      reportPending = true;
    }
    driveMotorSmooth(0, 0);
  }

  int error = (int)(smoothedTarget + 0.5f) - currentPosition;
  int absError = abs(error);

  if (autoMode && !touchActive) {
    // Manual takeover: if user moves strongly against command after GUI updates settle.
    if ((millis() - lastCommandMs) > COMMAND_GRACE_MS) {
      int commandDir = (error > 0) ? 1 : (error < 0 ? -1 : 0);
      int moveDir = (positionDelta > 0) ? 1 : (positionDelta < 0 ? -1 : 0);
      bool movedByHand = abs(positionDelta) >= MANUAL_MOVE_DELTA;
      bool farFromTarget = absError >= MANUAL_TAKEOVER_DELTA;
      if (movedByHand && farFromTarget && commandDir != 0 && moveDir != 0 && moveDir != commandDir) {
        autoMode = false;
        targetPosition = currentPosition;
        smoothedTarget = (float)currentPosition;
        driveMotorSmooth(0, 0);
        Serial.println(F("manual_takeover=1"));
        reportPending = true;
      }
    }
  }

  // Run motor control at a fixed 400 Hz cadence.
  unsigned long nowUs = micros();
  if ((uint32_t)(nowUs - lastControlUs) >= CONTROL_INTERVAL_US) {
    lastControlUs = nowUs;

    if (autoMode && !touchActive) {
      // Smoothly follow fast-changing GUI targets.
      smoothedTarget += TARGET_SMOOTH_ALPHA * ((float)targetPosition - smoothedTarget);
      int controlError = (int)(smoothedTarget + 0.5f) - currentPosition;
      int controlAbsError = abs(controlError);

      if (controlAbsError <= POS_DEADBAND && abs(targetPosition - currentPosition) <= POS_DEADBAND) {
        autoMode = false;
        smoothedTarget = (float)currentPosition;
        driveMotorSmooth(0, 0);
        reportPending = true;
      } else {
        int pwm = (int)(MOTOR_MIN_PWM + KP * controlAbsError);
        pwm = constrain(pwm, MOTOR_MIN_PWM, MOTOR_MAX_PWM);
        int direction = (controlError > 0) ? 1 : (controlError < 0 ? -1 : 0);
        driveMotorSmooth(direction, pwm);
      }
    } else {
      // Manual mode: motor off, target follows current.
      driveMotorSmooth(0, 0);
      if (abs(targetPosition - currentPosition) >= MANUAL_TARGET_SYNC_DEADBAND) {
        targetPosition = currentPosition;
        smoothedTarget = (float)currentPosition;
        reportPending = true;
      }
    }
  }

  reportStatus(currentPosition, false);
}
