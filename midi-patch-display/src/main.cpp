// ---------------------------------------------------------------------------
// midi-patch-display
//
// A CYD (ESP32-2432S028R) acting as the front panel for a headless FluidSynth
// rig on a Raspberry Pi. One USB cable carries power and data. The ESP32 radio
// is never started.
//
// Protocol: newline-delimited JSON, both directions.
//   Pi  -> CYD  {"t":"state","bank":0,"prog":4,"name":"Rhodes","sf":"FluidR3",
//                "batt":78,"vol":80,"index":12,"count":189,
//                "banks":[0,8,9,16,128],"midi":true,
//                "loop_state":"empty","loop_len":0,"metro_on":false,
//                "quantize":"off","can_undo":false,"can_redo":false,
//                "bars":"auto"}
//   Pi  -> CYD  {"t":"act"}                       MIDI activity blink
//   Pi  -> CYD  {"t":"toast","msg":"PANIC"}
//   CYD -> Pi   {"t":"hello","fw":"1.0.0"}
//   CYD -> Pi   {"t":"cmd","action":"patch_next"}
//   CYD -> Pi   {"t":"cmd","action":"volume_set","value":80}
//   CYD -> Pi   {"t":"cmd","action":"patch_select","bank":0,"prog":42}
//   CYD -> Pi   {"t":"cmd","action":"loop_record"}   toggles record/overdub
//   CYD -> Pi   {"t":"cmd","action":"loop_play"}     toggles play/stop
//   CYD -> Pi   {"t":"cmd","action":"loop_undo"}     step back one pass
//   CYD -> Pi   {"t":"cmd","action":"loop_redo"}     step forward one pass
//   CYD -> Pi   {"t":"cmd","action":"loop_clear"}    full wipe, long-press only
//   CYD -> Pi   {"t":"cmd","action":"metronome_toggle"}
//   CYD -> Pi   {"t":"cmd","action":"quantize_set","grid":"1/16"}
//   CYD -> Pi   {"t":"cmd","action":"loop_bars_set","bars":"4"}  or "auto"
//
// Wi-Fi picker (the Pi does the scanning and joining via NetworkManager):
//   Pi  -> CYD  "state" frames also carry "wifi": 0-4 bars, -1 = offline
//   Pi  -> CYD  {"t":"wifi","st":"list","cur":"Home","ip":"192.168.50.20",
//                "nets":[{"s":"Home","b":4,"l":true,"c":true}],"prompt":true}
//   Pi  -> CYD  {"t":"wifi","st":"busy","msg":"scanning..."}
//   Pi  -> CYD  {"t":"wifi","st":"ok","ssid":"Home","ip":"192.168.50.20"}
//   Pi  -> CYD  {"t":"wifi","st":"fail","msg":"wrong password?"}
//   CYD -> Pi   {"t":"cmd","action":"wifi_scan"}
//   CYD -> Pi   {"t":"wifi_join","ssid":"Home","psk":"..."}
// "prompt" opens the picker unasked - the Pi sends it on first boot, when it
// has no network and the touchscreen is the only way to give it one.
//
// Four screens - home, dial, loop, quantize - reached from a nav bar at the
// top of every screen (HOME/DIAL/LOOP/QNT), not by swiping. Dial: banks
// (from "banks") listed down the left, a 10-key + program display on the
// right. Loop: REC/PLAY, a small MET toggle and BARS cycler, and an
// UNDO/REDO split button for the single-track MIDI looper that actually
// lives in bridge.py (this panel never sees raw note data, just
// loop_state/loop_len/can_undo/can_redo/bars) - short-tap either half of
// the split button to step, hold it to fully clear instead. Quantize: the
// same grid options the web UI's drawer offers. "index"/"count" aren't
// used yet but are cheap to keep around for a future "N of M" readout.
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
static uint32_t lastVolSendMs = 0;

// screens - order matches ui.cpp's NAV_LABEL[] (HOME/DIAL/LOOP/QNT)
// The last two aren't on the nav bar - they're full-screen and reached from
// the status bar.
enum Screen { SCREEN_HOME, SCREEN_DIAL, SCREEN_LOOP, SCREEN_QUANTIZE,
              SCREEN_WIFI, SCREEN_KEYS };
static Screen   screen = SCREEN_HOME;

// dial screen
static int      dialPage = 0;
static int      dialBank = 0;
static char     dialProgBuf[4] = "";
static bool     dialKeyHeld = false;

// loop screen
static bool     loopKeyHeld = false;
// UNDO/REDO split button: tracked separately from loopKeyHeld because,
// unlike every other key here, it has to keep sampling every frame while
// held to detect a long-press (see LOOP_CLR_HOLD_MS).
static bool     clrPressActive = false;
static bool     clrPressIsRedo = false;
static uint32_t clrPressStartMs = 0;
static bool     clrLongFired = false;

// quantize screen
static bool     quantKeyHeld = false;

// wifi picker + keyboard
static WifiView wifi;
static bool     wifiKeyHeld = false;
static uint32_t wifiReturnAt = 0;     // after a successful join, back to home
static char     joinSsid[33] = "";
static char     keysText[64] = "";    // WPA2 passphrases top out at 63
static size_t   keysLen = 0;
static int      keysLayer = 0;
static int      keysHeld = -1;        // key index being pressed, for feedback

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

static void sendVolumeCmd(int pct) {
    JsonDocument d;
    d["t"] = "cmd";
    d["action"] = "volume_set";
    d["value"] = pct;
    serializeJson(d, Serial);
    Serial.println();
}

static void sendPatchSelectCmd(int bank, int prog) {
    JsonDocument d;
    d["t"] = "cmd";
    d["action"] = "patch_select";
    d["bank"] = bank;
    d["prog"] = prog;
    serializeJson(d, Serial);
    Serial.println();
}

static void sendQuantizeCmd(const char *grid) {
    JsonDocument d;
    d["t"] = "cmd";
    d["action"] = "quantize_set";
    d["grid"] = grid;
    serializeJson(d, Serial);
    Serial.println();
}

static void sendBarsCmd(const char *bars) {
    JsonDocument d;
    d["t"] = "cmd";
    d["action"] = "loop_bars_set";
    d["bars"] = bars;
    serializeJson(d, Serial);
    Serial.println();
}

static void sendWifiJoin(const char *ssid, const char *psk) {
    JsonDocument d;
    d["t"] = "wifi_join";
    d["ssid"] = ssid;
    d["psk"] = psk;
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

static void gotoScreen(Screen target);

static void handleWifiFrame(JsonDocument &d) {
    lastStateMs = millis();
    const char *st = d["st"] | "";

    if (!strcmp(st, "list")) {
        wifi.count = 0;
        for (JsonObject n : d["nets"].as<JsonArray>()) {
            if (wifi.count >= WIFI_MAX_NETS) break;
            WifiNet &w = wifi.nets[wifi.count++];
            strlcpy(w.ssid, n["s"] | "", sizeof(w.ssid));
            w.bars = n["b"] | 0;
            w.locked = n["l"] | false;
            w.current = n["c"] | false;
        }
        const char *cur = d["cur"] | "";
        if (cur[0]) {
            snprintf(wifi.status, sizeof(wifi.status), "%s  %s", cur, d["ip"] | "");
            wifi.level = 1;
        } else {
            strlcpy(wifi.status, "not connected", sizeof(wifi.status));
            wifi.level = 2;
        }
        wifi.busy = false;
    } else if (!strcmp(st, "busy")) {
        strlcpy(wifi.status, d["msg"] | "working...", sizeof(wifi.status));
        wifi.level = 0;
        wifi.busy = true;
    } else if (!strcmp(st, "ok")) {
        snprintf(wifi.status, sizeof(wifi.status), "connected  %s", d["ip"] | "");
        wifi.level = 1;
        wifi.busy = false;
        for (int i = 0; i < wifi.count; i++)
            wifi.nets[i].current = !strcmp(wifi.nets[i].ssid, d["ssid"] | "");
        wifiReturnAt = millis() + 2500;
        wakeBacklight();
    } else if (!strcmp(st, "fail")) {
        strlcpy(wifi.status, d["msg"] | "failed", sizeof(wifi.status));
        wifi.level = 2;
        wifi.busy = false;
        wakeBacklight();
    }

    // Never yank the user out of the keyboard mid-password.
    if ((d["prompt"] | false) && screen != SCREEN_KEYS && screen != SCREEN_WIFI) {
        wakeBacklight();
        gotoScreen(SCREEN_WIFI);
    } else if (screen == SCREEN_WIFI) {
        uiWifiRender(wifi);
    }
}

static void handleLine(const char *line) {
    JsonDocument d;
    if (deserializeJson(d, line)) return;      // ignore malformed frames

    const char *t = d["t"] | "";

    if (!strcmp(t, "state")) {
        int  oldBank = state.bank, oldProg = state.prog;
        state.bank = d["bank"] | -1;
        state.prog = d["prog"] | -1;
        state.batt = d["batt"] | -1;
        state.vol  = d["vol"] | -1;
        state.index = d["index"] | -1;
        state.count = d["count"] | -1;
        state.midi = d["midi"] | false;
        JsonArray banksArr = d["banks"];
        state.bankCount = 0;
        for (JsonVariant v : banksArr) {
            if (state.bankCount >= MAX_BANKS) break;
            state.banks[state.bankCount++] = v.as<int>();
        }
        strncpy(state.loopState, d["loop_state"] | "empty", sizeof(state.loopState) - 1);
        state.loopState[sizeof(state.loopState) - 1] = '\0';
        state.loopLen = d["loop_len"] | 0.0f;
        state.metroOn = d["metro_on"] | false;
        strncpy(state.quantize, d["quantize"] | "off", sizeof(state.quantize) - 1);
        state.quantize[sizeof(state.quantize) - 1] = '\0';
        state.canUndo = d["can_undo"] | false;
        state.canRedo = d["can_redo"] | false;
        strncpy(state.bars, d["bars"] | "auto", sizeof(state.bars) - 1);
        state.bars[sizeof(state.bars) - 1] = '\0';
        strncpy(state.name, d["name"] | "", sizeof(state.name) - 1);
        state.name[sizeof(state.name) - 1] = '\0';
        strncpy(state.sf, d["sf"] | "", sizeof(state.sf) - 1);
        state.sf[sizeof(state.sf) - 1] = '\0';
        state.wifi = d["wifi"] | -1;
        state.linked = true;
        lastStateMs = millis();
        // A patch change from the keyboard should light the screen back up.
        if (state.bank != oldBank || state.prog != oldProg) wakeBacklight();
        // The data above always applies regardless of which screen is up,
        // so patches.json edits etc. are ready the moment the user switches
        // to dial. The loop and quantize screens are the exception:
        // loop_state/loop_len/metro_on/quantize/bars are exactly what they
        // exist to show live, so they redraw on every state frame the same
        // as home does.
        if (screen == SCREEN_HOME) uiRender(state, false);
        else if (screen == SCREEN_LOOP) uiLoopRender(state, false);
        else if (screen == SCREEN_QUANTIZE) uiQuantRender(state, false);
    }
    else if (!strcmp(t, "act")) {
        uiMidiActivity();
        lastStateMs = millis();
    }
    else if (!strcmp(t, "toast")) {
        if (screen == SCREEN_HOME) uiToast(d["msg"] | "");
        wakeBacklight();
    }
    else if (!strcmp(t, "wifi")) {
        handleWifiFrame(d);
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
        case BTN_WIFI:    gotoScreen(SCREEN_WIFI); break;
        default: break;
    }
}

// Jumps directly to any screen from any other - what used to be four
// separate enter/exitXScreen() functions, back when the only way between
// screens was a swipe to an adjacent one. Resets every screen's "key
// currently held" flag so a gesture that was mid-flight on the screen you
// left can never leak onto the one you land on.
static void gotoScreen(Screen target) {
    if (target == screen) return;
    Screen from = screen;
    screen = target;
    dialKeyHeld = false;
    loopKeyHeld = false;
    clrPressActive = false;
    quantKeyHeld = false;
    // The finger that got us here is usually still down; on the Wi-Fi and
    // keyboard screens make it lift first, or it lands on whatever key sits
    // under the button it just pressed.
    wifiKeyHeld = true;
    keysHeld = -1;
    wifiReturnAt = 0;

    switch (target) {
        case SCREEN_HOME:
            uiBegin(&tft);
            uiRender(state, true);
            break;
        case SCREEN_DIAL:
            dialPage = 0;
            dialBank = state.bank < 0 ? 0 : state.bank;
            dialProgBuf[0] = '\0';
            uiDialBegin();
            uiDialRender(dialBank, dialProgBuf, dialPage, state, true);
            break;
        case SCREEN_LOOP:
            uiLoopBegin();
            uiLoopRender(state, true);
            break;
        case SCREEN_QUANTIZE:
            uiQuantBegin();
            uiQuantRender(state, true);
            break;
        case SCREEN_WIFI:
            // Coming back from the keyboard keeps whatever the join is
            // reporting; arriving fresh starts a scan.
            if (from != SCREEN_KEYS || !wifi.busy) {
                wifi.busy = true;
                strlcpy(wifi.status, "scanning...", sizeof(wifi.status));
                wifi.level = 0;
                sendCmd("wifi_scan");
            }
            uiWifiRender(wifi);
            return;   // full screen - no nav bar
        case SCREEN_KEYS:
            memset(keysText, 0, sizeof(keysText));
            keysLen = 0;
            keysLayer = 0;
            uiKeysBegin(joinSsid, keysLayer);
            return;
    }
    uiNavRender((int)target);
}

static void joinNetwork(const char *ssid, const char *psk) {
    sendWifiJoin(ssid, psk);
    // Don't keep the password around once it's sent.
    memset(keysText, 0, sizeof(keysText));
    keysLen = 0;
    snprintf(wifi.status, sizeof(wifi.status), "joining %s...", ssid);
    wifi.level = 0;
    wifi.busy = true;
    if (screen == SCREEN_WIFI) uiWifiRender(wifi);
    else gotoScreen(SCREEN_WIFI);
}

static void fireWifiKey(int key) {
    if (key == WIFI_BACK) {
        gotoScreen(SCREEN_HOME);
    } else if (key == WIFI_RESCAN) {
        // Not gated on busy: if a reply went missing, this is the way out.
        // The Pi answers "still working" if it really is.
        wifi.busy = true;
        strlcpy(wifi.status, "scanning...", sizeof(wifi.status));
        wifi.level = 0;
        sendCmd("wifi_scan");
        uiWifiRender(wifi);
    } else if (key >= 0 && key < wifi.count) {
        const WifiNet &n = wifi.nets[key];
        strlcpy(joinSsid, n.ssid, sizeof(joinSsid));
        if (n.locked) gotoScreen(SCREEN_KEYS);
        else joinNetwork(joinSsid, "");
    }
}

static void fireKeysKey(int code) {
    switch (code) {
        case KEY_BKSP:
            if (keysLen) keysText[--keysLen] = '\0';
            uiKeysText(keysText);
            break;
        case KEY_SHIFT:
            keysLayer = (keysLayer == 1) ? 0 : 1;
            uiKeysLayer(keysLayer);
            keysHeld = -1;    // the whole grid just redrew
            break;
        case KEY_SYM:
            keysLayer = (keysLayer == 2) ? 0 : 2;
            uiKeysLayer(keysLayer);
            keysHeld = -1;
            break;
        case KEY_CANCEL:
            memset(keysText, 0, sizeof(keysText));
            keysLen = 0;
            gotoScreen(SCREEN_WIFI);
            break;
        case KEY_OK:
            joinNetwork(joinSsid, keysText);
            break;
        default:
            if (code < 32 || code > 126 || keysLen >= sizeof(keysText) - 1) break;
            keysText[keysLen++] = (char)code;
            keysText[keysLen] = '\0';
            uiKeysText(keysText);
            // Shift is one-shot, like a phone keyboard.
            if (keysLayer == 1) {
                keysLayer = 0;
                uiKeysLayer(keysLayer);
                keysHeld = -1;
            }
            break;
    }
}

static void fireDialKey(int key) {
    if (key >= 0 && key <= 9) {
        size_t len = strlen(dialProgBuf);
        if (len < 3) {
            dialProgBuf[len] = char('0' + key);
            dialProgBuf[len + 1] = '\0';
        }
        uiDialRender(dialBank, dialProgBuf, dialPage, state, false);
    } else if (key == DIAL_CLR) {
        size_t len = strlen(dialProgBuf);
        if (len > 0) dialProgBuf[len - 1] = '\0';
        uiDialRender(dialBank, dialProgBuf, dialPage, state, false);
    } else if (key == DIAL_GO) {
        int prog = dialProgBuf[0] ? atoi(dialProgBuf) : 0;
        sendPatchSelectCmd(dialBank, prog);
        gotoScreen(SCREEN_HOME);
    } else if (key == DIAL_BANK_PAGE) {
        int pageCount = (state.bankCount + DIAL_BANK_ROWS_VISIBLE - 1) / DIAL_BANK_ROWS_VISIBLE;
        if (pageCount < 1) pageCount = 1;
        dialPage = (dialPage + 1) % pageCount;
        uiDialRender(dialBank, dialProgBuf, dialPage, state, true);
    } else if (key >= DIAL_BANK_ROW0 && key < DIAL_BANK_ROW0 + DIAL_BANK_ROWS_VISIBLE) {
        int row = key - DIAL_BANK_ROW0;
        int idx = dialPage * DIAL_BANK_ROWS_VISIBLE + row;
        if (idx < state.bankCount) {
            dialBank = state.banks[idx];
            uiDialRender(dialBank, dialProgBuf, dialPage, state, true);
        }
    }
}

// AUTO -> 1 -> 2 -> 4 -> 8 -> AUTO, based on whatever the panel is
// currently showing (which reflects bridge.py's actual setting).
static void fireLoopBarsCycle() {
    static const char *ORDER[5] = { "auto", "1", "2", "4", "8" };
    int idx = 0;
    for (int i = 0; i < 5; i++) {
        if (!strcmp(state.bars, ORDER[i])) { idx = i; break; }
    }
    sendBarsCmd(ORDER[(idx + 1) % 5]);
}

static void fireLoopKey(int key) {
    switch (key) {
        case LOOP_RECORD: sendCmd("loop_record"); break;
        case LOOP_PLAY:   sendCmd("loop_play");   break;
        case LOOP_UNDO:   sendCmd("loop_undo");   break;
        case LOOP_REDO:   sendCmd("loop_redo");   break;
        case LOOP_CLEAR:  sendCmd("loop_clear");  break;   // long-press only
        case LOOP_METRO:  sendCmd("metronome_toggle"); break;
        case LOOP_BARS:   fireLoopBarsCycle(); break;
        default: break;
    }
    // Stay on this screen either way - unlike the dial screen's GO, these
    // are controls you keep using (record, then play, then maybe clear),
    // not a one-shot action. The next state frame reflects the new state.
}

static void fireQuantKey(int key) {
    // Matches QuantKey's values and QUANT_OPTS's order in ui.cpp.
    static const char *WIRE[7] = { "off", "1/4", "1/8", "1/16", "1/32", "1/8t", "1/16t" };
    if (key < 0 || key > 6) return;
    sendQuantizeCmd(WIRE[key]);
    // Stay on screen - same reasoning as fireLoopKey, this is a setting you
    // might change more than once, not a one-shot action.
}

static void pollTouchHome() {
    int sx, sy;
    bool down = readTouch(sx, sy);
    uint32_t now = millis();

    if (down) {
        // Once a drag has started on the slider, keep tracking it by X
        // position alone until the finger actually lifts. Re-classifying the
        // zone every frame let ordinary vertical drift (or a one-frame touch
        // dropout) during a drag land above the strip and fire PANIC
        // mid-gesture, which also repaints this exact screen area - so a
        // stray drift looked like the slider silently resetting.
        if (heldBtn == BTN_VOLUME) {
            wakeBacklight();
            int pct = uiSliderPercentFromX(sx);
            if (pct != state.vol) {
                state.vol = pct;
                uiRender(state, false);
            }
            if (now - lastVolSendMs >= VOLUME_SEND_THROTTLE_MS) {
                lastVolSendMs = now;
                sendVolumeCmd(pct);
            }
            return;
        }

        if (heldBtn == BTN_NONE) {
            int navTarget = uiNavHitTest(sx, sy);
            if (navTarget >= 0) {
                if (now - lastFireMs < TOUCH_DEBOUNCE_MS) return;
                lastFireMs = now;
                wakeBacklight();
                gotoScreen((Screen)navTarget);
                return;
            }
        }

        int btn = uiHitTest(sx, sy);

        if (calMode && heldBtn == BTN_NONE) {
            static const char *NAMES[] = { "BANK-", "PREV", "NEXT", "BANK+", "PANIC", "VOLUME", "WIFI" };
            Serial.printf("#   -> zone %d (%s)\n", btn,
                          (btn >= 0 && btn <= 6) ? NAMES[btn] : "none");
        }

        if (btn == BTN_VOLUME) {
            wakeBacklight();
            heldBtn = BTN_VOLUME;
            int pct = uiSliderPercentFromX(sx);
            state.vol = pct;
            uiRender(state, false);
            lastVolSendMs = now;
            sendVolumeCmd(pct);
            return;
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
    } else {
        if (heldBtn != BTN_NONE) {
            if (heldBtn == BTN_VOLUME) {
                // Commit the final value even if the last drag frame was
                // skipped by the send throttle.
                sendVolumeCmd(state.vol);
            } else if (heldBtn <= 3) {
                uiPress(heldBtn, false);
            }
            heldBtn = BTN_NONE;
        }
    }
}

static void pollTouchDial() {
    int sx, sy;
    bool down = readTouch(sx, sy);
    uint32_t now = millis();

    if (down) {
        if (!dialKeyHeld) {
            int navTarget = uiNavHitTest(sx, sy);
            if (navTarget >= 0) {
                if (now - lastFireMs < TOUCH_DEBOUNCE_MS) return;
                lastFireMs = now;
                wakeBacklight();
                gotoScreen((Screen)navTarget);
                return;
            }

            int key = uiDialHitTest(sx, sy);
            if (key != DIAL_NONE) {
                if (now - lastFireMs < TOUCH_DEBOUNCE_MS) return;
                dialKeyHeld = true;
                lastFireMs = now;
                wakeBacklight();
                fireDialKey(key);
            }
        }
    } else {
        dialKeyHeld = false;
    }
}

static void pollTouchLoop() {
    int sx, sy;
    bool down = readTouch(sx, sy);
    uint32_t now = millis();

    if (down) {
        // UNDO/REDO long-press tracking runs independently of loopKeyHeld,
        // since (unlike every other key on this screen) it needs to keep
        // sampling every frame while the finger stays down, to catch the
        // moment it crosses the hold threshold rather than waiting for
        // release.
        if (clrPressActive) {
            if (!clrLongFired && now - clrPressStartMs >= LOOP_CLR_HOLD_MS) {
                clrLongFired = true;
                wakeBacklight();
                fireLoopKey(LOOP_CLEAR);
            }
            return;
        }

        if (!loopKeyHeld) {
            int navTarget = uiNavHitTest(sx, sy);
            if (navTarget >= 0) {
                if (now - lastFireMs < TOUCH_DEBOUNCE_MS) return;
                lastFireMs = now;
                wakeBacklight();
                gotoScreen((Screen)navTarget);
                return;
            }

            int key = uiLoopHitTest(sx, sy);
            if (key == LOOP_UNDO || key == LOOP_REDO) {
                if (now - lastFireMs < TOUCH_DEBOUNCE_MS) return;
                wakeBacklight();
                clrPressActive = true;
                clrPressIsRedo = (key == LOOP_REDO);
                clrPressStartMs = now;
                clrLongFired = false;
                return;   // fires on release (short tap) or hold-threshold (clear)
            }
            if (key != LOOP_NONE) {
                if (now - lastFireMs < TOUCH_DEBOUNCE_MS) return;
                loopKeyHeld = true;
                lastFireMs = now;
                wakeBacklight();
                fireLoopKey(key);
            }
        }
    } else {
        loopKeyHeld = false;
        if (clrPressActive) {
            if (!clrLongFired) {
                // Released before the hold threshold - a short tap.
                lastFireMs = now;
                fireLoopKey(clrPressIsRedo ? LOOP_REDO : LOOP_UNDO);
            }
            clrPressActive = false;
        }
    }
}

static void pollTouchQuantize() {
    int sx, sy;
    bool down = readTouch(sx, sy);
    uint32_t now = millis();

    if (down) {
        if (!quantKeyHeld) {
            int navTarget = uiNavHitTest(sx, sy);
            if (navTarget >= 0) {
                if (now - lastFireMs < TOUCH_DEBOUNCE_MS) return;
                lastFireMs = now;
                wakeBacklight();
                gotoScreen((Screen)navTarget);
                return;
            }

            int key = uiQuantHitTest(sx, sy);
            if (key != QUANT_NONE) {
                if (now - lastFireMs < TOUCH_DEBOUNCE_MS) return;
                quantKeyHeld = true;
                lastFireMs = now;
                wakeBacklight();
                fireQuantKey(key);
            }
        }
    } else {
        quantKeyHeld = false;
    }
}

static void pollTouchWifi() {
    int sx, sy;
    bool down = readTouch(sx, sy);
    uint32_t now = millis();

    if (down) {
        if (!wifiKeyHeld) {
            int key = uiWifiHitTest(sx, sy, wifi.count);
            if (key != WIFI_NONE) {
                if (now - lastFireMs < TOUCH_DEBOUNCE_MS) return;
                wifiKeyHeld = true;
                lastFireMs = now;
                wakeBacklight();
                fireWifiKey(key);
            }
        }
    } else {
        wifiKeyHeld = false;
    }
}

static void pollTouchKeys() {
    int sx, sy;
    bool down = readTouch(sx, sy);
    uint32_t now = millis();

    if (down) {
        if (keysHeld < 0 && !wifiKeyHeld) {
            int index = -1;
            int code = uiKeysHitTest(sx, sy, &index);
            if (code != KEY_NONE) {
                if (now - lastFireMs < TOUCH_DEBOUNCE_MS) return;
                lastFireMs = now;
                wifiKeyHeld = true;
                wakeBacklight();
                keysHeld = index;
                uiKeysPress(index, true);
                fireKeysKey(code);
            }
        }
    } else {
        if (keysHeld >= 0 && screen == SCREEN_KEYS) uiKeysPress(keysHeld, false);
        keysHeld = -1;
        wifiKeyHeld = false;
    }
}

static void pollTouch() {
    if (screen == SCREEN_WIFI) {
        pollTouchWifi();
    } else if (screen == SCREEN_KEYS) {
        pollTouchKeys();
    } else if (screen == SCREEN_DIAL) {
        pollTouchDial();
    } else if (screen == SCREEN_LOOP) {
        pollTouchLoop();
    } else if (screen == SCREEN_QUANTIZE) {
        pollTouchQuantize();
    } else {
        pollTouchHome();
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
    uiNavRender((int)SCREEN_HOME);

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
        if (screen == SCREEN_HOME) uiRender(state, false);
    }
    if (!state.linked && now - lastHelloMs > HELLO_RETRY_MS) {
        sendHello();
    }

    if (wifiReturnAt && now >= wifiReturnAt) {
        wifiReturnAt = 0;
        if (screen == SCREEN_WIFI) gotoScreen(SCREEN_HOME);
    }

    // Idle dimming - this shares the Pi's battery, so it matters.
#if BL_IDLE_MS > 0
    if (!backlightDim && now - lastTouchMs > BL_IDLE_MS) {
        backlightDim = true;
        setBacklight(BL_LEVEL_DIM);
    }
#endif

    // uiTick's toast-expiry path force-repaints the home screen's content
    // area - skip it entirely while another screen owns that space, or a
    // PANIC toast that outlives a screen switch would corrupt the display.
    if (screen == SCREEN_HOME) uiTick(state);
    delay(5);
}
