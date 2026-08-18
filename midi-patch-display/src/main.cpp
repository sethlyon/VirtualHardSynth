// ---------------------------------------------------------------------------
// midi-patch-display
//
// A CYD (ESP32-2432S028R) acting as the front panel for a headless FluidSynth
// rig on a Raspberry Pi. One USB cable carries power and data. The ESP32 radio
// is never started.
//
// Protocol: newline-delimited JSON, both directions.
//   Pi  -> CYD  {"t":"state","bank":0,"prog":4,"name":"Rhodes","sf":"FluidR3",
//                "batt":78,"midi":true}
//   Pi  -> CYD  {"t":"act"}                       MIDI activity blink
//   Pi  -> CYD  {"t":"toast","msg":"PANIC"}
//   CYD -> Pi   {"t":"hello","fw":"1.0.0"}
//   CYD -> Pi   {"t":"cmd","action":"patch_next"}
// ---------------------------------------------------------------------------

#include <Arduino.h>
#include <TFT_eSPI.h>
#include <XPT2046_Touchscreen.h>
#include <ArduinoJson.h>

#include "config.h"
#include "ui.h"

static TFT_eSPI tft;
static SPIClass touchSPI(VSPI);
static XPT2046_Touchscreen ts(PIN_TOUCH_CS, PIN_TOUCH_IRQ);

static PatchState state;

static char     rxBuf[512];
static size_t   rxLen = 0;

static uint32_t lastStateMs = 0;
static uint32_t lastHelloMs = 0;
static uint32_t lastTouchMs = 0;
static bool     backlightDim = false;
static bool     calMode = false;

// press tracking
static int      heldBtn      = BTN_NONE;
static uint32_t heldSinceMs  = 0;
static uint32_t lastFireMs   = 0;

// ---------------------------------------------------------------------------

static void setBacklight(uint8_t level) {
    ledcWrite(BL_PWM_CHANNEL, level);
}

static void wakeBacklight() {
    lastTouchMs = millis();
    if (backlightDim) {
        backlightDim = false;
        setBacklight(BL_LEVEL_FULL);
    }
}

static void sendCmd(const char *action) {
    JsonDocument d;
    d["t"] = "cmd";
    d["action"] = action;
    serializeJson(d, Serial);
    Serial.println();
}

static void sendHello() {
    JsonDocument d;
    d["t"] = "hello";
    d["fw"] = FW_VERSION;
    serializeJson(d, Serial);
    Serial.println();
    lastHelloMs = millis();
}

// ---------------------------------------------------------------------------

static void handleLine(const char *line) {
    JsonDocument d;
    if (deserializeJson(d, line)) return;      // ignore malformed frames

    const char *t = d["t"] | "";

    if (!strcmp(t, "state")) {
        int  oldBank = state.bank, oldProg = state.prog;
        state.bank = d["bank"] | -1;
        state.prog = d["prog"] | -1;
        state.batt = d["batt"] | -1;
        state.midi = d["midi"] | false;
        strncpy(state.name, d["name"] | "", sizeof(state.name) - 1);
        state.name[sizeof(state.name) - 1] = '\0';
        strncpy(state.sf, d["sf"] | "", sizeof(state.sf) - 1);
        state.sf[sizeof(state.sf) - 1] = '\0';
        state.linked = true;
        lastStateMs = millis();
        // A patch change from the keyboard should light the screen back up.
        if (state.bank != oldBank || state.prog != oldProg) wakeBacklight();
        uiRender(state, false);
    }
    else if (!strcmp(t, "act")) {
        uiMidiActivity();
        lastStateMs = millis();
    }
    else if (!strcmp(t, "toast")) {
        uiToast(d["msg"] | "");
        wakeBacklight();
    }
}

static void pollSerial() {
    while (Serial.available()) {
        char c = (char)Serial.read();
        if (c == '\n' || c == '\r') {
            if (rxLen) {
                rxBuf[rxLen] = '\0';
                if (!strncmp(rxBuf, "cal", 3)) {
                    calMode = !calMode;
                    Serial.printf("# calibration mode %s\n", calMode ? "ON" : "OFF");
                } else {
                    handleLine(rxBuf);
                }
                rxLen = 0;
            }
        } else if (rxLen < sizeof(rxBuf) - 1) {
            rxBuf[rxLen++] = c;
        } else {
            rxLen = 0;   // overlong line, resynchronise
        }
    }
}

// ---------------------------------------------------------------------------

static bool readTouch(int &sx, int &sy) {
    if (!ts.touched()) return false;
    TS_Point p = ts.getPoint();

    int rx = p.x, ry = p.y;
#if TOUCH_SWAP_XY
    int tmp = rx; rx = ry; ry = tmp;
#endif
    sx = map(rx, TS_RAW_MINX, TS_RAW_MAXX, 0, 320);
    sy = map(ry, TS_RAW_MINY, TS_RAW_MAXY, 0, 240);
#if TOUCH_INVERT_X
    sx = 320 - sx;
#endif
#if TOUCH_INVERT_Y
    sy = 240 - sy;
#endif
    sx = constrain(sx, 0, 319);
    sy = constrain(sy, 0, 239);

    if (calMode) {
        Serial.printf("# raw x=%4d y=%4d z=%4d  ->  screen x=%3d y=%3d\n",
                      p.x, p.y, p.z, sx, sy);
    }
    return true;
}

static void fireButton(int btn) {
    switch (btn) {
        case BTN_BANK_DN: sendCmd("bank_prev");  break;
        case BTN_PREV:    sendCmd("patch_prev"); break;
        case BTN_NEXT:    sendCmd("patch_next"); break;
        case BTN_BANK_UP: sendCmd("bank_next");  break;
        case BTN_PANIC:   sendCmd("panic"); uiToast("PANIC"); break;
        default: break;
    }
}

static void pollTouch() {
    int sx, sy;
    bool down = readTouch(sx, sy);
    uint32_t now = millis();

    if (down) {
        int btn = uiHitTest(sx, sy);

        if (calMode && heldBtn == BTN_NONE) {
            static const char *NAMES[] = { "BANK-", "PREV", "NEXT", "BANK+", "PANIC" };
            Serial.printf("#   -> zone %d (%s)\n", btn,
                          (btn >= 0 && btn <= 4) ? NAMES[btn] : "none");
        }

        if (heldBtn == BTN_NONE && btn != BTN_NONE) {
            if (now - lastFireMs < TOUCH_DEBOUNCE_MS) return;
            heldBtn     = btn;
            heldSinceMs = now;
            lastFireMs  = now;
            wakeBacklight();
            if (btn <= 3) uiPress(btn, true);
            fireButton(btn);
        }
        else if (heldBtn != BTN_NONE && heldBtn <= 3 && btn == heldBtn) {
            // auto-repeat while a transport button is held down
            if (now - heldSinceMs > TOUCH_REPEAT_MS &&
                now - lastFireMs  > TOUCH_REPEAT_MS) {
                lastFireMs = now;
                fireButton(heldBtn);
            }
        }
    } else if (heldBtn != BTN_NONE) {
        if (heldBtn <= 3) uiPress(heldBtn, false);
        heldBtn = BTN_NONE;
    }
}

// ---------------------------------------------------------------------------

void setup() {
    Serial.begin(SERIAL_BAUD);

    ledcSetup(BL_PWM_CHANNEL, BL_PWM_FREQ, BL_PWM_BITS);
    ledcAttachPin(PIN_BACKLIGHT, BL_PWM_CHANNEL);
    setBacklight(BL_LEVEL_FULL);

    tft.init();
    tft.setRotation(SCREEN_ROTATION);

    touchSPI.begin(PIN_TOUCH_CLK, PIN_TOUCH_MISO, PIN_TOUCH_MOSI, PIN_TOUCH_CS);
    ts.begin(touchSPI);
    ts.setRotation(SCREEN_ROTATION);

    uiBegin(&tft);
    strncpy(state.name, "waiting for pi", sizeof(state.name) - 1);
    uiRender(state, true);

    lastTouchMs = millis();
    sendHello();
}

void loop() {
    pollSerial();
    pollTouch();

    uint32_t now = millis();

    // Link watchdog - no frames from the bridge means it died or was unplugged.
    if (state.linked && now - lastStateMs > LINK_TIMEOUT_MS) {
        state.linked = false;
        uiRender(state, false);
    }
    if (!state.linked && now - lastHelloMs > HELLO_RETRY_MS) {
        sendHello();
    }

    // Idle dimming - this shares the Pi's battery, so it matters.
#if BL_IDLE_MS > 0
    if (!backlightDim && now - lastTouchMs > BL_IDLE_MS) {
        backlightDim = true;
        setBacklight(BL_LEVEL_DIM);
    }
#endif

    uiTick(state);
    delay(5);
}
