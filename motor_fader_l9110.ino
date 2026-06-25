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
    - ?                : print help
*/

const uint8_t FADER_PIN = A0;
const uint8_t TOUCH_PIN = 7;
const uint8_t MOTOR_IA_PIN = 5;  // L9110 IA
const uint8_t MOTOR_IB_PIN = 6;  // L9110 IB

// If your touch pin behaves opposite, set this to false.
const bool TOUCH_ACTIVE_LOW = true;
const bool USE_TOUCH = false;

// If motor goes the wrong direction, flip this.
const bool MOTOR_FORWARD_INCREASES_ADC = true;

// Control tuning
const int DEAD_BAND = 6;                 // no movement when error is small
const uint8_t MIN_PWM = 115;             // minimum PWM that reliably moves motor
const uint8_t MAX_PWM = 235;             // limit max power for quieter movement
const float KP = 0.35f;                  // proportional gain
const float FILTER_ALPHA = 0.18f;        // 0..1, larger = faster but noisier
const unsigned long TOUCH_HOLD_MS = 180; // short delay after touch release

int targetPosition = 512;
float filteredPosition = 512.0f;
unsigned long touchHoldUntil = 0;
unsigned long lastPrintMs = 0;

String serialLine;

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
  if (!MOTOR_FORWARD_INCREASES_ADC) {
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

void printHelp() {
  Serial.println(F("Commands:"));
  Serial.println(F("  0..1023  -> set target"));
  Serial.println(F("  min      -> target 0"));
  Serial.println(F("  max      -> target 1023"));
  Serial.println(F("  center   -> target 512"));
  Serial.println(F("  stop     -> stop and hold current position"));
  Serial.println(F("  read     -> print current/target/touch"));
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
    targetPosition = 0;
    Serial.println(F("target=0"));
    return;
  }
  if (line == "max") {
    targetPosition = 1023;
    Serial.println(F("target=1023"));
    return;
  }
  if (line == "center") {
    targetPosition = 512;
    Serial.println(F("target=512"));
    return;
  }
  if (line == "stop") {
    targetPosition = currentPosition;
    motorStop();
    Serial.println(F("stopped"));
    return;
  }
  if (line == "read") {
    Serial.print(F("pos="));
    Serial.print(currentPosition);
    Serial.print(F(" target="));
    Serial.print(targetPosition);
    Serial.print(F(" touch="));
    Serial.println(isTouched() ? F("1") : F("0"));
    return;
  }

  int parsed = 0;
  if (parseNumber(line, parsed)) {
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

  Serial.println(F("Motorized fader ready."));
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
  } else if (millis() < touchHoldUntil) {
    motorStop();
  } else {
    int error = targetPosition - currentPosition;
    int absError = abs(error);

    if (absError <= DEAD_BAND) {
      motorStop();
    } else {
      int pwm = (int)(MIN_PWM + (KP * absError));
      pwm = constrain(pwm, MIN_PWM, MAX_PWM);
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
    Serial.println(touched ? F("1") : F("0"));
  }
}
