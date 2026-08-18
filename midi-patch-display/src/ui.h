#pragma once
#include <Arduino.h>
#include <TFT_eSPI.h>

// Everything the screen knows about the rig. Populated from "state" frames
// sent by the Pi bridge.
struct PatchState {
    int  bank   = -1;
    int  prog   = -1;
    int  batt   = -1;       // -1 = unknown / no battery module
    bool midi   = false;    // keyboard port present
    bool linked = false;    // bridge heartbeat is alive
    char name[40] = "";
    char sf[28]   = "";
};

enum ButtonId {
    BTN_NONE    = -1,
    BTN_BANK_DN = 0,
    BTN_PREV    = 1,
    BTN_NEXT    = 2,
    BTN_BANK_UP = 3,
    BTN_PANIC   = 4     // the big centre area
};

void uiBegin(TFT_eSPI *tft);
void uiRender(const PatchState &s, bool force);
void uiPress(int btn, bool down);
void uiToast(const char *msg);
void uiMidiActivity();
void uiTick(const PatchState &s);
int  uiHitTest(int x, int y);
