#include "ui.h"
#include "config.h"

// ---- palette (RGB565) -----------------------------------------------------
#define C_BG      0x0861
#define C_BAR     0x18C3
#define C_TEXT    0xFFFF
#define C_DIM     0x8410
#define C_ACCENT  0x07FF
#define C_WARN    0xFD20
#define C_BAD     0xF800
#define C_OK      0x07E0
#define C_BTN     0x2124
#define C_BTN_DN  0x045F
#define C_LINE    0x39E7

// ---- layout ---------------------------------------------------------------
#define SCR_W     320
#define SCR_H     240
#define BAR_H     26

// Nav bar: HOME/DIAL/LOOP/QNT, directly under the main status bar, on every
// screen. Content on every screen starts below it (CONTENT_Y), which is
// the one thing that changed on all four screens - each one's own layout
// is otherwise just shifted down by NAV_H from where it used to start
// right under BAR_H.
#define NAV_Y      BAR_H
#define NAV_H      20
#define NAV_W      (SCR_W / 4)
#define CONTENT_Y  (BAR_H + NAV_H)

#define BTN_Y     (CONTENT_Y + 150)
#define BTN_H     (SCR_H - BTN_Y)
#define BTN_W     (SCR_W / 4)

// Volume slider: a strip above the button row. Takes the space the old
// "tap here for panic" hint used to occupy - panic is still reachable by
// tapping the name/bank area above this strip, it just isn't labelled
// on-screen any more.
#define SLIDER_Y       (CONTENT_Y + 122)
#define SLIDER_H       22
#define SLIDER_LABEL_W 34
#define SLIDER_BAR_X   (16 + SLIDER_LABEL_W)
#define SLIDER_BAR_W   (SCR_W - SLIDER_BAR_X - 16)
// Vertical hit zone is taller than the drawn bar so it's easy to land a
// finger on without being pixel-precise.
#define SLIDER_HIT_Y0  (SLIDER_Y - 6)

// Panic moved into the status bar as a small labelled button, between the
// soundfont/link text (which clears out to x=206) and the MIDI dot (~x=268).
// It used to be "tap anywhere in the main area", which was too easy to hit
// by accident while using the volume slider just below it.
#define PANIC_BTN_X   210
#define PANIC_BTN_Y   3
#define PANIC_BTN_W   48
#define PANIC_BTN_H   (BAR_H - 6)

// Dial screen: banks the current soundfont has down the left, a 10-key +
// program display on the right. Fills the whole screen below the nav bar -
// the volume slider and BANK/PREV/NEXT/BANK+ row are hidden while it's up.
#define DIAL_BANK_COL_W    88
#define DIAL_BANK_HEADER_H 24
#define DIAL_BANK_ROW_H    ((SCR_H - CONTENT_Y - DIAL_BANK_HEADER_H) / DIAL_BANK_ROWS_VISIBLE)

#define DIAL_DISPLAY_Y     CONTENT_Y
#define DIAL_DISPLAY_H     44

#define DIAL_KEY_X0        DIAL_BANK_COL_W
#define DIAL_KEY_Y0        (DIAL_DISPLAY_Y + DIAL_DISPLAY_H)
#define DIAL_KEY_W         ((SCR_W - DIAL_BANK_COL_W) / 3)
#define DIAL_KEY_H         ((SCR_H - DIAL_KEY_Y0) / 4)

// Loop screen: a status readout up top - with small MET (top-right) and
// BARS (top-left) toggles/cyclers carved out of its corners, same idea as
// PANIC in the main status bar - and three big buttons below.
#define LOOP_DISPLAY_Y   CONTENT_Y
#define LOOP_DISPLAY_H   58
#define LOOP_MET_W       52
#define LOOP_MET_H       20
#define LOOP_MET_X       (SCR_W - LOOP_MET_W - 8)
#define LOOP_MET_Y       (LOOP_DISPLAY_Y + 4)
#define LOOP_BARS_W      52
#define LOOP_BARS_H      20
#define LOOP_BARS_X      8
#define LOOP_BARS_Y      (LOOP_DISPLAY_Y + 4)
// Status text lives below the MET/BARS row.
#define LOOP_TEXT_Y0     (LOOP_MET_Y + LOOP_MET_H + 2)
#define LOOP_BTN_Y       (LOOP_DISPLAY_Y + LOOP_DISPLAY_H)
#define LOOP_BTN_H       (SCR_H - LOOP_BTN_Y)
#define LOOP_BTN_W       (SCR_W / 3)

// Quantize screen: a header strip, then a grid of the same snap options the
// web UI's drawer offers.
#define QUANT_DISPLAY_Y  CONTENT_Y
#define QUANT_DISPLAY_H  32
#define QUANT_GRID_Y     (QUANT_DISPLAY_Y + QUANT_DISPLAY_H)
#define QUANT_GRID_H     (SCR_H - QUANT_GRID_Y)
#define QUANT_COLS       4
#define QUANT_ROWS       2
#define QUANT_CELL_W     (SCR_W / QUANT_COLS)
#define QUANT_CELL_H     (QUANT_GRID_H / QUANT_ROWS)
#define QUANT_OPT_COUNT  7

static TFT_eSPI  *g = nullptr;
static PatchState prev;
static bool       primed = false;

static uint32_t   toastUntil = 0;
static char       toastMsg[28] = "";
static uint32_t   midiBlinkUntil = 0;
static bool       midiDotLit = false;

static const char *BTN_LABEL[4] = { "BANK-", "PREV", "NEXT", "BANK+" };
static const char *NAV_LABEL[4] = { "HOME", "DIAL", "LOOP", "QNT" };

struct QuantOpt { const char *label; const char *wireKey; };
// Grid order, left to right / top to bottom - matches QuantKey's values, and
// the fixed set bridge.py's LOOP_QUANTIZE_GRIDS actually accepts.
static const QuantOpt QUANT_OPTS[QUANT_OPT_COUNT] = {
    { "OFF",   "off"   },
    { "1/4",   "1/4"   },
    { "1/8",   "1/8"   },
    { "1/16",  "1/16"  },
    { "1/32",  "1/32"  },
    { "1/8T",  "1/8t"  },
    { "1/16T", "1/16t" },
};

// ---------------------------------------------------------------------------
// Nav bar - shared across every screen
// ---------------------------------------------------------------------------

void uiNavRender(int current) {
    if (!g) return;
    for (int i = 0; i < 4; i++) {
        int x = i * NAV_W;
        bool isCurrent = (i == current);
        uint16_t f = isCurrent ? C_ACCENT : C_BAR;
        uint16_t t = isCurrent ? C_BG : C_DIM;
        g->fillRect(x, NAV_Y, NAV_W, NAV_H, f);
        g->setTextDatum(MC_DATUM);
        g->setTextColor(t, f);
        g->setTextPadding(0);
        g->drawString(NAV_LABEL[i], x + NAV_W / 2, NAV_Y + NAV_H / 2, 1);
    }
    g->drawFastVLine(NAV_W, NAV_Y, NAV_H, C_LINE);
    g->drawFastVLine(NAV_W * 2, NAV_Y, NAV_H, C_LINE);
    g->drawFastVLine(NAV_W * 3, NAV_Y, NAV_H, C_LINE);
    g->drawFastHLine(0, NAV_Y + NAV_H, SCR_W, C_LINE);
}

int uiNavHitTest(int x, int y) {
    if (x < 0 || x >= SCR_W || y < NAV_Y || y >= NAV_Y + NAV_H) return -1;
    int col = x / NAV_W;
    if (col < 0) col = 0; if (col > 3) col = 3;
    return col;
}

// ---------------------------------------------------------------------------

static void drawButton(int i, bool down) {
    int x = i * BTN_W;
    g->fillRoundRect(x + 3, BTN_Y + 3, BTN_W - 6, BTN_H - 6, 6,
                     down ? C_BTN_DN : C_BTN);
    g->drawRoundRect(x + 3, BTN_Y + 3, BTN_W - 6, BTN_H - 6, 6, C_LINE);
    g->setTextDatum(MC_DATUM);
    g->setTextColor(down ? C_BG : C_TEXT, down ? C_BTN_DN : C_BTN);
    g->setTextPadding(0);
    g->drawString(BTN_LABEL[i], x + BTN_W / 2, BTN_Y + BTN_H / 2, 2);
}

static void drawBatteryIcon(int x, int y, int pct) {
    // 22x11 body with a 2px nub on the right
    g->drawRect(x, y, 22, 11, C_DIM);
    g->fillRect(x + 22, y + 3, 2, 5, C_DIM);
    g->fillRect(x + 1, y + 1, 20, 9, C_BG);
    if (pct < 0) {
        g->setTextDatum(MC_DATUM);
        g->setTextColor(C_DIM, C_BG);
        g->drawString("?", x + 11, y + 5, 1);
        return;
    }
    int fill = (pct * 20) / 100;
    if (fill < 0)  fill = 0;
    if (fill > 20) fill = 20;
    uint16_t c = (pct <= 15) ? C_BAD : (pct <= 35) ? C_WARN : C_OK;
    if (fill > 0) g->fillRect(x + 1, y + 1, fill, 9, c);
}

static void renderBar(const PatchState &s, bool force) {
    bool sfChanged   = force || strcmp(s.sf, prev.sf) != 0;
    bool battChanged = force || s.batt != prev.batt;
    bool linkChanged = force || s.linked != prev.linked;

    if (force) g->fillRect(0, 0, SCR_W, BAR_H, C_BAR);

    if (force) {
        g->drawRoundRect(PANIC_BTN_X, PANIC_BTN_Y, PANIC_BTN_W, PANIC_BTN_H, 3, C_WARN);
        g->setTextDatum(MC_DATUM);
        g->setTextColor(C_WARN, C_BAR);
        g->setTextPadding(0);
        g->drawString("PANIC", PANIC_BTN_X + PANIC_BTN_W / 2,
                      PANIC_BTN_Y + PANIC_BTN_H / 2, 1);
    }

    if (sfChanged || linkChanged) {
        g->setTextDatum(ML_DATUM);
        g->setTextColor(s.linked ? C_DIM : C_BAD, C_BAR);
        g->setTextPadding(200);
        g->drawString(s.linked ? (s.sf[0] ? s.sf : "no soundfont") : "LINK DOWN",
                      6, BAR_H / 2, 2);
    }
    if (battChanged || force) {
        drawBatteryIcon(SCR_W - 30, (BAR_H - 11) / 2, s.batt);
    }
}

static void renderMidiDot(const PatchState &s, bool force) {
    bool lit = (millis() < midiBlinkUntil);
    if (!force && lit == midiDotLit) return;
    midiDotLit = lit;
    uint16_t c = !s.midi ? C_BAD : (lit ? C_ACCENT : C_LINE);
    g->fillCircle(SCR_W - 48, BAR_H / 2, 4, c);
}

static void renderMain(const PatchState &s, bool force) {
    if (millis() < toastUntil) return;   // toast owns the area right now

    bool nameChanged = force || strcmp(s.name, prev.name) != 0;
    bool numChanged  = force || s.bank != prev.bank || s.prog != prev.prog;

    if (nameChanged) {
        // Shrink to the smaller font if a long preset name would overflow.
        uint8_t font = 4;
        if (g->textWidth(s.name, 4) > SCR_W - 16) font = 2;
        g->setTextDatum(MC_DATUM);
        g->setTextColor(C_ACCENT, C_BG);
        g->setTextPadding(SCR_W);
        // Clear both possible font heights so a font switch leaves no residue.
        g->fillRect(0, CONTENT_Y + 36, SCR_W, 40, C_BG);
        g->drawString(s.name[0] ? s.name : "-", SCR_W / 2, CONTENT_Y + 56, font);
    }

    if (numChanged) {
        char buf[40];
        snprintf(buf, sizeof(buf), "BANK %03d      PROG %03d",
                 s.bank < 0 ? 0 : s.bank, s.prog < 0 ? 0 : s.prog);
        g->setTextDatum(MC_DATUM);
        g->setTextColor(C_TEXT, C_BG);
        g->setTextPadding(SCR_W);
        g->drawString(buf, SCR_W / 2, CONTENT_Y + 100, 2);
    }

}

static void renderSlider(const PatchState &s, bool force) {
    bool changed = force || s.vol != prev.vol;
    if (!changed) return;

    int pct = s.vol < 0 ? 0 : (s.vol > 100 ? 100 : s.vol);

    if (force) {
        g->setTextDatum(ML_DATUM);
        g->setTextColor(C_DIM, C_BG);
        g->setTextPadding(0);
        g->drawString("VOL", 16, SLIDER_Y + SLIDER_H / 2, 2);
        g->drawRoundRect(SLIDER_BAR_X, SLIDER_Y, SLIDER_BAR_W, SLIDER_H, 4, C_LINE);
    }

    int innerW = SLIDER_BAR_W - 4;
    int fill = (pct * innerW) / 100;
    g->fillRoundRect(SLIDER_BAR_X + 2, SLIDER_Y + 2, innerW, SLIDER_H - 4, 3, C_BG);
    if (fill > 0) {
        g->fillRoundRect(SLIDER_BAR_X + 2, SLIDER_Y + 2, fill, SLIDER_H - 4, 3, C_ACCENT);
    }
}

// ---------------------------------------------------------------------------

void uiBegin(TFT_eSPI *tft) {
    g = tft;
    g->fillScreen(C_BG);
    g->fillRect(0, 0, SCR_W, BAR_H, C_BAR);
    g->drawFastHLine(0, BAR_H, SCR_W, C_LINE);
    g->drawFastHLine(0, BTN_Y, SCR_W, C_LINE);
    for (int i = 0; i < 4; i++) drawButton(i, false);
    primed = false;
}

void uiRender(const PatchState &s, bool force) {
    if (!g) return;
    if (!primed) { force = true; primed = true; }
    renderBar(s, force);
    renderMidiDot(s, force);
    renderMain(s, force);
    renderSlider(s, force);
    prev = s;
}

void uiPress(int btn, bool down) {
    if (!g || btn < 0 || btn > 3) return;
    drawButton(btn, down);
}

void uiToast(const char *msg) {
    if (!g) return;
    strncpy(toastMsg, msg, sizeof(toastMsg) - 1);
    toastMsg[sizeof(toastMsg) - 1] = '\0';
    toastUntil = millis() + 900;
    // Below the nav bar only - it isn't repainted here, so erasing into it
    // would leave it blank until the next screen switch redraws it.
    g->fillRect(0, CONTENT_Y, SCR_W, BTN_Y - CONTENT_Y, C_BG);
    g->setTextDatum(MC_DATUM);
    g->setTextColor(C_WARN, C_BG);
    g->setTextPadding(SCR_W);
    g->drawString(toastMsg, SCR_W / 2, (CONTENT_Y + BTN_Y) / 2, 4);
}

void uiMidiActivity() {
    midiBlinkUntil = millis() + 120;
}

void uiTick(const PatchState &s) {
    if (!g) return;
    // Repaint the main area the moment a toast expires.
    if (toastUntil && millis() >= toastUntil) {
        toastUntil = 0;
        g->fillRect(0, CONTENT_Y, SCR_W, BTN_Y - CONTENT_Y, C_BG);
        uiRender(s, true);
        return;
    }
    renderMidiDot(s, false);
}

int uiHitTest(int x, int y) {
    if (x < 0 || x >= SCR_W || y < 0 || y >= SCR_H) return BTN_NONE;
    if (y >= BTN_Y) {
        int i = x / BTN_W;
        return (i < 0) ? BTN_NONE : (i > 3 ? 3 : i);
    }
    if (y >= SLIDER_HIT_Y0) return BTN_VOLUME;
    if (y < BAR_H) {
        if (x >= PANIC_BTN_X && x < PANIC_BTN_X + PANIC_BTN_W) return BTN_PANIC;
        return BTN_NONE;
    }
    return BTN_NONE;   // nav bar and the name/bank display - no tap action here
}

int uiSliderPercentFromX(int x) {
    int rel = x - SLIDER_BAR_X;
    if (rel < 0) rel = 0;
    if (rel > SLIDER_BAR_W) rel = SLIDER_BAR_W;
    return (rel * 100) / SLIDER_BAR_W;
}

// ---------------------------------------------------------------------------
// Dial screen
// ---------------------------------------------------------------------------

void uiDialBegin() {
    if (!g) return;
    // Only clear below the nav bar - the main status bar (PANIC, link
    // status, battery, MIDI dot) and the nav bar itself stay exactly as
    // they were; the caller repaints the nav bar separately.
    g->fillRect(0, CONTENT_Y, SCR_W, SCR_H - CONTENT_Y, C_BG);
    g->drawFastHLine(0, CONTENT_Y, SCR_W, C_LINE);
    g->drawFastVLine(DIAL_BANK_COL_W, CONTENT_Y, SCR_H - CONTENT_Y, C_LINE);
    g->drawFastHLine(DIAL_BANK_COL_W, DIAL_KEY_Y0, SCR_W - DIAL_BANK_COL_W, C_LINE);
    primed = false;   // force a full home-screen repaint on the way back out
}

static void dialDrawBankHeader(int page, int pageCount) {
    g->fillRect(0, CONTENT_Y, DIAL_BANK_COL_W, DIAL_BANK_HEADER_H, C_BAR);
    g->setTextDatum(MC_DATUM);
    g->setTextColor(C_DIM, C_BAR);
    g->setTextPadding(0);
    char buf[16];
    if (pageCount > 1) {
        snprintf(buf, sizeof(buf), "BANKS %d/%d", page + 1, pageCount);
    } else {
        snprintf(buf, sizeof(buf), "BANKS");
    }
    g->drawString(buf, DIAL_BANK_COL_W / 2, CONTENT_Y + DIAL_BANK_HEADER_H / 2, 1);
}

static void dialDrawBankRow(int row, int bankValue, bool selected, bool present) {
    int y = CONTENT_Y + DIAL_BANK_HEADER_H + row * DIAL_BANK_ROW_H;
    uint16_t bg = selected ? C_ACCENT : C_BG;
    uint16_t fg = selected ? C_BG : (present ? C_TEXT : C_LINE);
    g->fillRect(1, y, DIAL_BANK_COL_W - 1, DIAL_BANK_ROW_H - 1, bg);
    if (present) {
        char buf[8];
        snprintf(buf, sizeof(buf), "%03d", bankValue);
        g->setTextDatum(MC_DATUM);
        g->setTextColor(fg, bg);
        g->setTextPadding(0);
        g->drawString(buf, DIAL_BANK_COL_W / 2, y + DIAL_BANK_ROW_H / 2, 2);
    }
}

// Grid order, left to right / top to bottom.
static const int DIAL_KEY_ORDER[12] = { 1, 2, 3, 4, 5, 6, 7, 8, 9, DIAL_CLR, 0, DIAL_GO };

static void dialDrawKeypad() {
    for (int row = 0; row < 4; row++) {
        for (int col = 0; col < 3; col++) {
            int i = row * 3 + col;
            int x = DIAL_KEY_X0 + col * DIAL_KEY_W;
            int y = DIAL_KEY_Y0 + row * DIAL_KEY_H;
            int key = DIAL_KEY_ORDER[i];
            uint16_t fill = (key == DIAL_GO) ? C_ACCENT : C_BTN;
            uint16_t text = (key == DIAL_GO) ? C_BG : C_TEXT;
            g->fillRoundRect(x + 3, y + 3, DIAL_KEY_W - 6, DIAL_KEY_H - 6, 6, fill);
            g->drawRoundRect(x + 3, y + 3, DIAL_KEY_W - 6, DIAL_KEY_H - 6, 6, C_LINE);
            g->setTextDatum(MC_DATUM);
            g->setTextColor(text, fill);
            g->setTextPadding(0);
            char label[4];
            if (key >= 0 && key <= 9) snprintf(label, sizeof(label), "%d", key);
            else if (key == DIAL_CLR) snprintf(label, sizeof(label), "CLR");
            else snprintf(label, sizeof(label), "GO");
            g->drawString(label, x + DIAL_KEY_W / 2, y + DIAL_KEY_H / 2, 4);
        }
    }
}

void uiDialRender(int selectedBank, const char *progBuf, int page,
                  const PatchState &s, bool force) {
    if (!g) return;

    int pageCount = (s.bankCount + DIAL_BANK_ROWS_VISIBLE - 1) / DIAL_BANK_ROWS_VISIBLE;
    if (pageCount < 1) pageCount = 1;

    if (force) {
        dialDrawBankHeader(page, pageCount);
        for (int row = 0; row < DIAL_BANK_ROWS_VISIBLE; row++) {
            int idx = page * DIAL_BANK_ROWS_VISIBLE + row;
            bool present = idx < s.bankCount;
            int val = present ? s.banks[idx] : 0;
            dialDrawBankRow(row, val, present && val == selectedBank, present);
        }
        dialDrawKeypad();
    }

    // Display box: selected bank + program digits typed so far.
    g->fillRect(DIAL_KEY_X0, DIAL_DISPLAY_Y, SCR_W - DIAL_KEY_X0, DIAL_DISPLAY_H, C_BAR);
    char buf[24];
    snprintf(buf, sizeof(buf), "BANK %03d", selectedBank < 0 ? 0 : selectedBank);
    g->setTextDatum(ML_DATUM);
    g->setTextColor(C_DIM, C_BAR);
    g->setTextPadding(0);
    g->drawString(buf, DIAL_KEY_X0 + 10, DIAL_DISPLAY_Y + 14, 2);

    g->setTextDatum(ML_DATUM);
    g->setTextColor(C_ACCENT, C_BAR);
    g->setTextPadding(SCR_W);
    g->drawString(progBuf[0] ? progBuf : "PROG ---",
                  DIAL_KEY_X0 + 10, DIAL_DISPLAY_Y + 32, 4);
}

int uiDialHitTest(int x, int y) {
    if (x < 0 || x >= SCR_W || y < CONTENT_Y || y >= SCR_H) return DIAL_NONE;

    if (x < DIAL_BANK_COL_W) {
        if (y < CONTENT_Y + DIAL_BANK_HEADER_H) return DIAL_BANK_PAGE;
        int row = (y - CONTENT_Y - DIAL_BANK_HEADER_H) / DIAL_BANK_ROW_H;
        if (row < 0 || row >= DIAL_BANK_ROWS_VISIBLE) return DIAL_NONE;
        return DIAL_BANK_ROW0 + row;
    }

    if (y < DIAL_KEY_Y0) return DIAL_NONE;   // the display box itself
    int col = (x - DIAL_KEY_X0) / DIAL_KEY_W;
    int row = (y - DIAL_KEY_Y0) / DIAL_KEY_H;
    if (col < 0) col = 0; if (col > 2) col = 2;
    if (row < 0) row = 0; if (row > 3) row = 3;
    return DIAL_KEY_ORDER[row * 3 + col];
}

// ---------------------------------------------------------------------------
// Loop screen
// ---------------------------------------------------------------------------

void uiLoopBegin() {
    if (!g) return;
    g->fillRect(0, CONTENT_Y, SCR_W, SCR_H - CONTENT_Y, C_BG);
    g->drawFastHLine(0, CONTENT_Y, SCR_W, C_LINE);
    g->drawFastHLine(0, LOOP_BTN_Y, SCR_W, C_LINE);
    g->drawFastVLine(LOOP_BTN_W, LOOP_BTN_Y, SCR_H - LOOP_BTN_Y, C_LINE);
    g->drawFastVLine(LOOP_BTN_W * 2, LOOP_BTN_Y, SCR_H - LOOP_BTN_Y, C_LINE);
    primed = false;   // force a full home-screen repaint on the way back out
}

static void loopDrawButton(int col, const char *label, uint16_t fill, bool enabled) {
    int x = col * LOOP_BTN_W;
    uint16_t f = enabled ? fill : C_BTN;
    uint16_t t = enabled ? C_BG : C_LINE;
    g->fillRoundRect(x + 4, LOOP_BTN_Y + 4, LOOP_BTN_W - 8, LOOP_BTN_H - 8, 8, f);
    g->drawRoundRect(x + 4, LOOP_BTN_Y + 4, LOOP_BTN_W - 8, LOOP_BTN_H - 8, 8, C_LINE);
    g->setTextDatum(MC_DATUM);
    g->setTextColor(t, f);
    g->setTextPadding(0);
    g->drawString(label, x + LOOP_BTN_W / 2, LOOP_BTN_Y + LOOP_BTN_H / 2, 2);
}

// Small pill in the top-right corner of the status strip, same idea as
// PANIC in the main bar - reuses the enabled/disabled visual as an on/off
// indicator, green+lit when the click is running, dim when it's not.
static void loopDrawMetToggle(bool on) {
    uint16_t f = on ? C_OK : C_BTN;
    uint16_t t = on ? C_BG : C_LINE;
    g->fillRoundRect(LOOP_MET_X, LOOP_MET_Y, LOOP_MET_W, LOOP_MET_H, 4, f);
    g->drawRoundRect(LOOP_MET_X, LOOP_MET_Y, LOOP_MET_W, LOOP_MET_H, 4, C_LINE);
    g->setTextDatum(MC_DATUM);
    g->setTextColor(t, f);
    g->setTextPadding(0);
    g->drawString("MET", LOOP_MET_X + LOOP_MET_W / 2, LOOP_MET_Y + LOOP_MET_H / 2, 1);
}

// Mirrors MET in the top-left corner. Tap to cycle AUTO -> 1 -> 2 -> 4 ->
// 8 -> AUTO; takes effect on the *next* base recording, which then
// auto-finishes on its own after that many bars instead of waiting for a
// REC press.
static void loopDrawBarsPill(const char *barsWire) {
    bool active = strcmp(barsWire, "auto") != 0;
    char label[8];
    if (active) snprintf(label, sizeof(label), "%sBAR", barsWire);
    else snprintf(label, sizeof(label), "AUTO");
    uint16_t f = active ? C_ACCENT : C_BTN;
    uint16_t t = active ? C_BG : C_LINE;
    g->fillRoundRect(LOOP_BARS_X, LOOP_BARS_Y, LOOP_BARS_W, LOOP_BARS_H, 4, f);
    g->drawRoundRect(LOOP_BARS_X, LOOP_BARS_Y, LOOP_BARS_W, LOOP_BARS_H, 4, C_LINE);
    g->setTextDatum(MC_DATUM);
    g->setTextColor(t, f);
    g->setTextPadding(0);
    g->drawString(label, LOOP_BARS_X + LOOP_BARS_W / 2, LOOP_BARS_Y + LOOP_BARS_H / 2, 1);
}

// The old CLR slot (column 2), now split into two independently-enabled
// halves stacked top (UNDO) over bottom (REDO). A long-press anywhere on
// it still fires a full clear - see LOOP_CLR_HOLD_MS in main.cpp.
static void loopDrawUndoRedo(bool undoEnabled, bool redoEnabled) {
    int x = 2 * LOOP_BTN_W;
    int y = LOOP_BTN_Y + 4;
    int w = LOOP_BTN_W - 8;
    int h = LOOP_BTN_H - 8;
    int halfH = h / 2;
    uint16_t fUndo = undoEnabled ? C_WARN : C_BTN;
    uint16_t tUndo = undoEnabled ? C_BG : C_LINE;
    uint16_t fRedo = redoEnabled ? C_WARN : C_BTN;
    uint16_t tRedo = redoEnabled ? C_BG : C_LINE;

    g->fillRoundRect(x + 4, y, w, h, 8, C_BTN);
    g->fillRect(x + 4, y, w, halfH, fUndo);
    g->fillRect(x + 4, y + halfH, w, h - halfH, fRedo);
    g->drawRoundRect(x + 4, y, w, h, 8, C_LINE);
    g->drawFastHLine(x + 6, y + halfH, w - 4, C_LINE);

    g->setTextDatum(MC_DATUM);
    g->setTextPadding(0);
    g->setTextColor(tUndo, fUndo);
    g->drawString("UNDO", x + 4 + w / 2, y + halfH / 2, 1);
    g->setTextColor(tRedo, fRedo);
    g->drawString("REDO", x + 4 + w / 2, y + halfH + (h - halfH) / 2, 1);
}

void uiLoopRender(const PatchState &s, bool force) {
    if (!g) return;
    (void)force;   // this screen is cheap enough to always fully repaint

    bool counting    = !strcmp(s.loopState, "counting_in");
    bool recording   = !strcmp(s.loopState, "recording");
    bool overdubbing = !strcmp(s.loopState, "overdubbing");
    bool isPlaying   = !strcmp(s.loopState, "playing");
    bool isStopped   = !strcmp(s.loopState, "stopped");
    bool hasContent  = strcmp(s.loopState, "empty") != 0;
    bool capturing   = recording || overdubbing;

    g->fillRect(0, LOOP_DISPLAY_Y, SCR_W, LOOP_DISPLAY_H, C_BG);
    loopDrawBarsPill(s.bars);
    loopDrawMetToggle(s.metroOn);

    char buf[32];
    if (counting)          snprintf(buf, sizeof(buf), "GET READY...");
    else if (recording)    snprintf(buf, sizeof(buf), "RECORDING...");
    else if (overdubbing)  snprintf(buf, sizeof(buf), "OVERDUBBING...");
    else if (!hasContent)  snprintf(buf, sizeof(buf), "LOOP EMPTY");
    else snprintf(buf, sizeof(buf), "%s  %.1fs", isPlaying ? "PLAYING" : "STOPPED", s.loopLen);
    g->setTextDatum(MC_DATUM);
    g->setTextColor(capturing ? C_BAD : C_ACCENT, C_BG);
    g->setTextPadding(SCR_W);
    g->drawString(buf, SCR_W / 2, (LOOP_TEXT_Y0 + LOOP_BTN_Y) / 2, 4);

    // REC doubles as OVERDUB any time there's already a loop to layer onto
    // - playing or paused, same key. Pressing it while stopped resumes
    // playback first (so overdub always has something to overdub against),
    // which is also how you swap sounds mid-loop: stop, browse to a
    // different patch, hit REC. Only an empty loop gets a plain "REC".
    const char *recLabel = capturing ? "STOP" : (hasContent ? "DUB" : "REC");
    loopDrawButton(0, recLabel, C_BAD, !counting);
    loopDrawButton(1, isPlaying ? "STOP" : "PLAY", C_ACCENT, isPlaying || isStopped);
    loopDrawUndoRedo(s.canUndo, s.canRedo);
}

int uiLoopHitTest(int x, int y) {
    if (x < 0 || x >= SCR_W || y < 0 || y >= SCR_H) return LOOP_NONE;
    if (y >= LOOP_BTN_Y) {
        int col = x / LOOP_BTN_W;
        if (col < 0) col = 0; if (col > 2) col = 2;
        if (col == 2) {
            int localY = y - LOOP_BTN_Y;
            return (localY < LOOP_BTN_H / 2) ? LOOP_UNDO : LOOP_REDO;
        }
        return col;   // LOOP_RECORD=0, LOOP_PLAY=1
    }
    if (x >= LOOP_MET_X && x < LOOP_MET_X + LOOP_MET_W &&
        y >= LOOP_MET_Y && y < LOOP_MET_Y + LOOP_MET_H) {
        return LOOP_METRO;
    }
    if (x >= LOOP_BARS_X && x < LOOP_BARS_X + LOOP_BARS_W &&
        y >= LOOP_BARS_Y && y < LOOP_BARS_Y + LOOP_BARS_H) {
        return LOOP_BARS;
    }
    return LOOP_NONE;
}

// ---------------------------------------------------------------------------
// Quantize screen
// ---------------------------------------------------------------------------

void uiQuantBegin() {
    if (!g) return;
    g->fillRect(0, CONTENT_Y, SCR_W, SCR_H - CONTENT_Y, C_BG);
    g->drawFastHLine(0, CONTENT_Y, SCR_W, C_LINE);
    g->drawFastHLine(0, QUANT_GRID_Y, SCR_W, C_LINE);
    g->drawFastHLine(0, QUANT_GRID_Y + QUANT_CELL_H, SCR_W, C_LINE);
    for (int c = 1; c < QUANT_COLS; c++) {
        g->drawFastVLine(c * QUANT_CELL_W, QUANT_GRID_Y, QUANT_GRID_H, C_LINE);
    }
    primed = false;   // force a full home-screen repaint on the way back out
}

static void quantDrawCell(int key, bool selected) {
    int col = key % QUANT_COLS;
    int row = key / QUANT_COLS;
    int x = col * QUANT_CELL_W;
    int y = QUANT_GRID_Y + row * QUANT_CELL_H;
    uint16_t fill = selected ? C_ACCENT : C_BTN;
    uint16_t text = selected ? C_BG : C_TEXT;
    g->fillRoundRect(x + 4, y + 4, QUANT_CELL_W - 8, QUANT_CELL_H - 8, 8, fill);
    g->drawRoundRect(x + 4, y + 4, QUANT_CELL_W - 8, QUANT_CELL_H - 8, 8, C_LINE);
    g->setTextDatum(MC_DATUM);
    g->setTextColor(text, fill);
    g->setTextPadding(0);
    g->drawString(QUANT_OPTS[key].label, x + QUANT_CELL_W / 2, y + QUANT_CELL_H / 2, 2);
}

void uiQuantRender(const PatchState &s, bool force) {
    if (!g) return;
    (void)force;   // cheap enough to always fully repaint, same as loop

    int selected = 0;
    for (int i = 0; i < QUANT_OPT_COUNT; i++) {
        if (!strcmp(s.quantize, QUANT_OPTS[i].wireKey)) { selected = i; break; }
    }

    g->fillRect(0, QUANT_DISPLAY_Y, SCR_W, QUANT_DISPLAY_H, C_BG);
    char buf[24];
    snprintf(buf, sizeof(buf), "QUANTIZE: %s", QUANT_OPTS[selected].label);
    g->setTextDatum(MC_DATUM);
    g->setTextColor(C_ACCENT, C_BG);
    g->setTextPadding(SCR_W);
    g->drawString(buf, SCR_W / 2, QUANT_DISPLAY_Y + QUANT_DISPLAY_H / 2, 4);

    for (int i = 0; i < QUANT_OPT_COUNT; i++) quantDrawCell(i, i == selected);
}

int uiQuantHitTest(int x, int y) {
    if (x < 0 || x >= SCR_W || y < QUANT_GRID_Y || y >= SCR_H) return QUANT_NONE;
    int col = x / QUANT_CELL_W;
    int row = (y - QUANT_GRID_Y) / QUANT_CELL_H;
    if (col < 0 || col >= QUANT_COLS || row < 0 || row >= QUANT_ROWS) return QUANT_NONE;
    int key = row * QUANT_COLS + col;
    return key < QUANT_OPT_COUNT ? key : QUANT_NONE;
}
