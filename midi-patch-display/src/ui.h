#pragma once
#include <Arduino.h>
#include <TFT_eSPI.h>

#define MAX_BANKS 24

// Everything the screen knows about the rig. Populated from "state" frames
// sent by the Pi bridge.
struct PatchState {
    int  bank   = -1;
    int  prog   = -1;
    int  batt   = -1;       // -1 = unknown / no battery module
    int  vol    = -1;       // 0-100, -1 = unknown (no state frame yet)
    int  index  = -1;       // this patch's position in the full sorted list
    int  count  = -1;       // total patches, -1 = unknown (no state frame yet)
    bool midi   = false;    // keyboard port present
    bool linked = false;    // bridge heartbeat is alive
    char name[40] = "";
    char sf[28]   = "";
    int  banks[MAX_BANKS];  // distinct bank numbers, for the dial screen's list
    int  bankCount = 0;
    // empty, counting_in, recording, playing, overdubbing, stopped
    char loopState[12] = "empty";
    float loopLen = 0;             // seconds, meaningless while loopState is empty
    bool  metroOn = false;
    char  quantize[8] = "off";     // off, 1/4, 1/8, 1/16, 1/32, 1/8t, 1/16t
    bool  canUndo = false;
    bool  canRedo = false;
    char  bars[6] = "auto";        // auto, 1, 2, 4, 8 - fixed base-recording length
    int   wifi = -1;               // 0-4 signal bars, -1 = not on Wi-Fi
};

enum ButtonId {
    BTN_NONE    = -1,
    BTN_BANK_DN = 0,
    BTN_PREV    = 1,
    BTN_NEXT    = 2,
    BTN_BANK_UP = 3,
    BTN_PANIC   = 4,    // small status-bar button
    BTN_VOLUME  = 5,    // slider strip, above the buttons
    BTN_WIFI    = 6     // soundfont name / Wi-Fi icon in the status bar
};

// ---------------------------------------------------------------------------
// Nav bar - a row of 4 buttons (HOME/DIAL/LOOP/QUANTIZE) directly under the
// main status bar, present on every screen, current one highlighted and
// inert. Replaces the old swipe-to-navigate gesture: it worked, but jumping
// straight to any screen from any other beats always going through the two
// adjacent ones. Index order matches main.cpp's Screen enum (0=home,
// 1=dial, 2=loop, 3=quantize) - ui.cpp doesn't know that enum, so this side
// just deals in plain ints.
// ---------------------------------------------------------------------------

void uiNavRender(int current);
int  uiNavHitTest(int x, int y);

// ---------------------------------------------------------------------------
// Home screen
// ---------------------------------------------------------------------------

void uiBegin(TFT_eSPI *tft);
void uiRender(const PatchState &s, bool force);
void uiPress(int btn, bool down);
void uiToast(const char *msg);
void uiMidiActivity();
void uiTick(const PatchState &s);
int  uiHitTest(int x, int y);
int  uiSliderPercentFromX(int x);

// ---------------------------------------------------------------------------
// Dial screen - reached via the nav bar. Banks the current soundfont
// actually has down the left, a 10-key + program display on the right.
// Selecting a bank only changes what the keypad targets; nothing plays
// until GO.
// ---------------------------------------------------------------------------

enum DialKey {
    DIAL_NONE     = -1,
    // 0-9 are literally the digit values
    DIAL_CLR      = 10,
    DIAL_GO       = 11,
    DIAL_BANK_ROW0 = 20,   // 20..20+DIAL_BANK_ROWS_VISIBLE-1
    DIAL_BANK_PAGE = 30,   // tapped the header - advance to the next page
};
#define DIAL_BANK_ROWS_VISIBLE 5

void uiDialBegin();
// progBuf: digits typed so far for the program field, "" if none yet.
void uiDialRender(int selectedBank, const char *progBuf, int page,
                  const PatchState &s, bool force);
int  uiDialHitTest(int x, int y);

// ---------------------------------------------------------------------------
// Loop screen - reached via the nav bar. A single-track MIDI looper: REC
// starts a fresh recording (after a 4-beat count-in) or, once a loop is
// playing, layers an overdub onto it - same key, state-dependent. PLAY/STOP
// toggles playback. MET (top-right corner) and BARS (top-left corner) are
// small toggles/cyclers rather than full-width buttons - BARS cycles
// AUTO -> 1 -> 2 -> 4 -> 8 -> AUTO, pinning the *next* base recording to
// that many bars (auto-finishing on its own) instead of however long you
// hold it. The old CLR slot is now split into UNDO (top half) and REDO
// (bottom half) - short tap either half for one step, repeatable. A
// long-press anywhere on that same split button still does a full,
// immediate, no-going-back CLEAR (see LOOP_CLR_HOLD_MS in main.cpp) - the
// panel doesn't distinguish tap-vs-hold itself, uiLoopHitTest() only ever
// reports which half was touched. The actual recording/playback/
// click/undo-redo all live on the Pi (bridge.py's Looper and Metronome
// classes) - this is just the control surface, driven purely off the
// loop_state string in each "state" frame.
// ---------------------------------------------------------------------------

enum LoopKey {
    LOOP_NONE = -1,
    LOOP_RECORD = 0, LOOP_PLAY = 1, LOOP_UNDO = 2, LOOP_REDO = 3,
    LOOP_METRO = 4, LOOP_BARS = 5,
    LOOP_CLEAR = 6,   // LOOP_CLEAR only ever fires from a long-press
};

void uiLoopBegin();
void uiLoopRender(const PatchState &s, bool force);
int  uiLoopHitTest(int x, int y);

// ---------------------------------------------------------------------------
// Quantize screen - reached via the nav bar. A grid of the same snap
// options the web UI's drawer offers; picking one sends quantize_set the
// same way the web control channel does.
// ---------------------------------------------------------------------------

enum QuantKey {
    QUANT_NONE = -1,
    QUANT_OFF = 0, QUANT_1_4 = 1, QUANT_1_8 = 2, QUANT_1_16 = 3,
    QUANT_1_32 = 4, QUANT_1_8T = 5, QUANT_1_16T = 6,
};

void uiQuantBegin();
void uiQuantRender(const PatchState &s, bool force);
int  uiQuantHitTest(int x, int y);

// ---------------------------------------------------------------------------
// Wi-Fi picker - reached by tapping the soundfont name in the home screen's
// status bar, or opened by the Pi itself (a "prompt" frame) when it has no
// network on first boot. Full screen, no nav bar: BACK returns home. The
// list and all the connecting happen on the Pi (NetworkManager); this side
// only shows what "wifi" frames say and sends back the choice.
// ---------------------------------------------------------------------------

#define WIFI_MAX_NETS 6

struct WifiNet {
    char ssid[33];
    int  bars;          // 0-4
    bool locked;        // needs a password
    bool current;       // the network the Pi is on now
};

struct WifiView {
    WifiNet nets[WIFI_MAX_NETS];
    int     count = 0;
    char    status[44] = "";
    int     level = 0;      // 0 = info, 1 = good, 2 = bad
    bool    busy = false;   // scanning/joining - shown in place of an empty list
};

enum WifiKey {
    WIFI_NONE   = -1,
    // 0..WIFI_MAX_NETS-1 are list rows
    WIFI_BACK   = 10,
    WIFI_RESCAN = 11,
};

void uiWifiRender(const WifiView &w);
int  uiWifiHitTest(int x, int y, int count);

// ---------------------------------------------------------------------------
// Keyboard - password entry for a locked network. Three layers (lower,
// upper, symbols) of a 10x4 grid plus CANCEL / space / JOIN. Keys are
// identified by index (0-39 grid, 40-42 bottom row) for press feedback;
// uiKeysHitTest() also returns what the key means.
// ---------------------------------------------------------------------------

enum KeyCode {
    KEY_NONE   = 0,
    KEY_SHIFT  = 1,
    KEY_SYM    = 2,
    KEY_CANCEL = 3,
    KEY_OK     = 4,
    KEY_BKSP   = 8,
    // anything else is the printable character itself
};

void uiKeysBegin(const char *ssid, int layer);
void uiKeysLayer(int layer);           // 0 = lower, 1 = upper, 2 = symbols
void uiKeysText(const char *text);
void uiKeysPress(int index, bool down);
int  uiKeysHitTest(int x, int y, int *index);
