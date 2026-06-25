/*
  ALPS motorized fader + L9110 driver (Arduino Uno/Nano)

  Wiring used in this sketch:
    - Fader wiper (pin 2) -> A0
    - Fader end pins       -> 5V and GND
    - Fader touch pin (T)  -> D7
    - L9110 IA             -> D5
    - L9110 IB             -> D6
    - L9110 VCC            -> 5V (or motor-rated supply)
    - L9110 GND            -> GND (shared with Arduino)

  Serial commands (115200 baud):
    - number 0..1023   : set target position
    - min              : target 0
    - max              : target 1023
    - center           : target 512
    - stop             : stop motor and set target=current
    - read             : print current position
    - flip             : invert motor direction logic
    - dir0 / dir1      : set direction logic explicitly
    - ?                : print help
*/

const uint8_t FADER_PIN = A0;
const uint8_t TOUCH_PIN = 7;
const uint8_t MOTOR_IA_PIN = 5;  // L9110 IA
const uint8_t MOTOR_IB_PIN = 6;  // L9110 IB

// Touch disabled by default (no resistor needed for testing).
const bool TOUCH_ACTIVE_LOW = true;
const bool USE_TOUCH = false;

// Direction may be auto-corrected once motion is observed.
bool motorForwardIncreasesAdc = true;

// Control tuning
const int DEAD_BAND = 6;                     // no movement when error is small
const uint8_t MIN_PWM = 120;                 // minimum PWM that reliably moves motor
const uint8_t MAX_PWM = 225;                 // limit max power for quieter movement
const float KP = 0.34f;                      // proportional gain
const float FILTER_ALPHA = 0.18f;            // 0..1, larger = faster but noisier
const unsigned long TOUCH_HOLD_MS = 180;     // short delay after touch release
const unsigned long STALL_TIMEOUT_MS = 700;  // stop if no movement while driving
const unsigned long MAX_DRIVE_MS = 1400;     // hard cap on single drive attempt
const unsigned long FAULT_COOLDOWN_MS = 900; // cooldown after safety stop
const unsigned long INVERT_CHECK_MS = 180;   // direction auto-detect window
const uint8_t MOVEMENT_DELTA = 2;            // minimum ADC change to count movement
const int ENDSTOP_LOW = 1;                   // near-physical low endpoint
const int ENDSTOP_HIGH = 1022;               // near-physical high endpoint

int targetPosition = 512;
float filteredPosition = 512.0f;
unsigned long touchHoldUntil = 0;
unsigned long lastPrintMs = 0;
unsigned long faultUntilMs = 0;

bool driveActive = false;
int driveStartPosition = 0;
int driveExpectedSign = 0;
int lastMovementPosition = 0;
unsigned long driveStartMs = 0;
unsigned long lastMovementMs = 0;

String serialLine;

int signum(int value) {
  if (value > 0) return 1;
  if (value < 0) return -1;
  return 0;
}

int readPosition() {
  int raw = analogRead(FADER_PIN);
  filteredPosition += FILTER_ALPHA * (raw - filteredPosition);
  return (int)(filteredPosition + 0.5f);
}

bool isTouched() {
  if (!USE_TOUCH) return false;
  int v = digitalRead(TOUCH_PIN);
  return TOUCH_ACTIVE_LOW ? (v == LOW) : (v == HIGH);
}

void motorStop() {
  analogWrite(MOTOR_IA_PIN, 0);
  analogWrite(MOTOR_IB_PIN, 0);
}

void motorDrive(bool towardHigherPosition, uint8_t pwm) {
  bool forward = towardHigherPosition;
  if (!motorForwardIncreasesAdc) {
    forward = !forward;
  }

  if (forward) {
    analogWrite(MOTOR_IA_PIN, pwm);
    analogWrite(MOTOR_IB_PIN, 0);
  } else {
    analogWrite(MOTOR_IA_PIN, 0);
    analogWrite(MOTOR_IB_PIN, pwm);
  }
}

void clearDriveState() {
  driveActive = false;
  driveExpectedSign = 0;
}

void armForNewCommand() {
  faultUntilMs = 0;
  clearDriveState();
}

void latchSafetyStop(const __FlashStringHelper *reason, int holdPosition) {
  targetPosition = holdPosition;
  faultUntilMs = millis() + FAULT_COOLDOWN_MS;
  motorStop();
  clearDriveState();
  Serial.print(F("fault="));
  Serial.println(reason);
}

void printHelp() {
  Serial.println(F("Commands:"));
  Serial.println(F("  0..1023  -> set target"));
  Serial.println(F("  min      -> target 0"));
  Serial.println(F("  max      -> target 1023"));
  Serial.println(F("  center   -> target 512"));
  Serial.println(F("  stop     -> stop and hold current position"));
  Serial.println(F("  read     -> print current/target/touch"));
  Serial.println(F("  flip     -> invert direction"));
  Serial.println(F("  dir0     -> forward decreases ADC"));
  Serial.println(F("  dir1     -> forward increases ADC"));
  Serial.println(F("  ?        -> help"));
}

bool parseNumber(const String &s, int &out) {
  if (s.length() == 0) return false;

  for (size_t i = 0; i < s.length(); i++) {
    char c = s.charAt(i);
    if (c < '0' || c > '9') {
      return false;
    }
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
    armForNewCommand();
    targetPosition = 0;
    Serial.println(F("target=0"));
    return;
  }
  if (line == "max") {
    armForNewCommand();
    targetPosition = 1023;
    Serial.println(F("target=1023"));
    return;
  }
  if (line == "center") {
    armForNewCommand();
    targetPosition = 512;
    Serial.println(F("target=512"));
    return;
  }
  if (line == "stop") {
    armForNewCommand();
    targetPosition = currentPosition;
    motorStop();
    Serial.println(F("stopped"));
    return;
  }
  if (line == "flip") {
    motorForwardIncreasesAdc = !motorForwardIncreasesAdc;
    armForNewCommand();
    Serial.print(F("dir="));
    Serial.println(motorForwardIncreasesAdc ? 1 : 0);
    return;
  }
  if (line == "dir0") {
    motorForwardIncreasesAdc = false;
    armForNewCommand();
    Serial.println(F("dir=0"));
    return;
  }
  if (line == "dir1") {
    motorForwardIncreasesAdc = true;
    armForNewCommand();
    Serial.println(F("dir=1"));
    return;
  }
  if (line == "read") {
    Serial.print(F("pos="));
    Serial.print(currentPosition);
    Serial.print(F(" target="));
    Serial.print(targetPosition);
    Serial.print(F(" touch="));
    Serial.print(isTouched() ? F("1") : F("0"));
    Serial.print(F(" dir="));
    Serial.println(motorForwardIncreasesAdc ? 1 : 0);
    return;
  }

  int parsed = 0;
  if (parseNumber(line, parsed)) {
    armForNewCommand();
    targetPosition = parsed;
    Serial.print(F("target="));
    Serial.println(targetPosition);
    return;
  }

  Serial.println(F("Unknown command. Type ?"));
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
  pinMode(MOTOR_IA_PIN, OUTPUT);
  pinMode(MOTOR_IB_PIN, OUTPUT);
  pinMode(TOUCH_PIN, INPUT_PULLUP);

  motorStop();

  Serial.begin(115200);
  delay(100);

  int initial = analogRead(FADER_PIN);
  filteredPosition = (float)initial;
  targetPosition = initial;

  Serial.println(F("Motorized fader safe mode ready."));
  printHelp();
}

void loop() {
  int currentPosition = readPosition();
  handleSerial(currentPosition);

  bool touched = isTouched();
  if (touched) {
    // Follow the hand while touched, so the motor never fights user motion.
    targetPosition = currentPosition;
    touchHoldUntil = millis() + TOUCH_HOLD_MS;
    motorStop();
    clearDriveState();
  } else if (millis() < touchHoldUntil) {
    motorStop();
    clearDriveState();
  } else if (millis() < faultUntilMs) {
    motorStop();
    clearDriveState();
  } else {
    int error = targetPosition - currentPosition;
    int absError = abs(error);

    if (absError <= DEAD_BAND) {
      motorStop();
      clearDriveState();
    } else {
      // Prevent pushing harder into a hard endpoint.
      if ((currentPosition <= ENDSTOP_LOW && error < 0) ||
          (currentPosition >= ENDSTOP_HIGH && error > 0)) {
        latchSafetyStop(F("endstop"), currentPosition);
        return;
      }

      int pwm = (int)(MIN_PWM + (KP * absError));
      pwm = constrain(pwm, MIN_PWM, MAX_PWM);
      unsigned long now = millis();

      if (!driveActive) {
        driveActive = true;
        driveStartPosition = currentPosition;
        lastMovementPosition = currentPosition;
        driveExpectedSign = signum(error);
        driveStartMs = now;
        lastMovementMs = now;
      }

      if (abs(currentPosition - lastMovementPosition) >= MOVEMENT_DELTA) {
        lastMovementPosition = currentPosition;
        lastMovementMs = now;
      }

      // If movement starts in the wrong direction, auto flip and retry.
      if ((now - driveStartMs) >= INVERT_CHECK_MS) {
        int net = currentPosition - driveStartPosition;
        if (abs(net) >= MOVEMENT_DELTA) {
          int actualSign = signum(net);
          if (actualSign != 0 && driveExpectedSign != 0 && actualSign != driveExpectedSign) {
            motorForwardIncreasesAdc = !motorForwardIncreasesAdc;
            motorStop();
            clearDriveState();
            faultUntilMs = now + 220;
            Serial.println(F("auto_invert=1"));
            return;
          }
        }
      }

      if ((now - lastMovementMs) > STALL_TIMEOUT_MS) {
        latchSafetyStop(F("stall"), currentPosition);
        return;
      }

      if ((now - driveStartMs) > MAX_DRIVE_MS) {
        latchSafetyStop(F("timeout"), currentPosition);
        return;
      }

      motorDrive(error > 0, (uint8_t)pwm);
    }
  }

  // Lightweight status stream for tuning.
  if (millis() - lastPrintMs >= 200) {
    lastPrintMs = millis();
    Serial.print(F("pos="));
    Serial.print(currentPosition);
    Serial.print(F(" target="));
    Serial.print(targetPosition);
    Serial.print(F(" touch="));
    Serial.print(touched ? F("1") : F("0"));
    Serial.print(F(" dir="));
    Serial.println(motorForwardIncreasesAdc ? 1 : 0);
  }
}
