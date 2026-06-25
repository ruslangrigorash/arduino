/*
  Emergency motor stop firmware
  Forces L9110 control pins LOW continuously.
*/

const uint8_t MOTOR_IA_PIN = 5;
const uint8_t MOTOR_IB_PIN = 6;
const uint8_t FADER_PIN = A0;

void setup() {
  pinMode(MOTOR_IA_PIN, OUTPUT);
  pinMode(MOTOR_IB_PIN, OUTPUT);
  digitalWrite(MOTOR_IA_PIN, LOW);
  digitalWrite(MOTOR_IB_PIN, LOW);

  Serial.begin(115200);
  delay(100);
  Serial.println(F("EMERGENCY STOP ACTIVE"));
}

void loop() {
  // Keep motor outputs off.
  digitalWrite(MOTOR_IA_PIN, LOW);
  digitalWrite(MOTOR_IB_PIN, LOW);

  // Optional telemetry so you can verify analog position changes by hand.
  static unsigned long lastPrint = 0;
  if (millis() - lastPrint >= 250) {
    lastPrint = millis();
    Serial.print(F("pos="));
    Serial.println(analogRead(FADER_PIN));
  }
}
