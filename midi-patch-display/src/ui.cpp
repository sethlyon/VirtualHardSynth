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
#define BTN_Y     176
#define BTN_H     (SCR_H - BTN_Y)
#define BTN_W     (SCR_W / 4)

static TFT_eSPI  *g = nullptr;
static PatchState prev;
static bool       primed = false;

static uint32_t   toastUntil = 0;
static char       toastMsg[28] = "";
static uint32_t   midiBlinkUntil = 0;
static bool       midiDotLit = false;

static const char *BTN_LABEL[4] = { "BANK-", "PREV", "NEXT", "BANK+" };

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
        g->fillRect(0, 62, SCR_W, 40, C_BG);
        g->drawString(s.name[0] ? s.name : "-", SCR_W / 2, 82, font);
    }

    if (numChanged) {
        char buf[40];
        snprintf(buf, sizeof(buf), "BANK %03d      PROG %03d",
                 s.bank < 0 ? 0 : s.bank, s.prog < 0 ? 0 : s.prog);
        g->setTextDatum(MC_DATUM);
        g->setTextColor(C_TEXT, C_BG);
        g->setTextPadding(SCR_W);
        g->drawString(buf, SCR_W / 2, 126, 2);
    }

    if (force) {
        g->setTextDatum(MC_DATUM);
        g->setTextColor(C_LINE, C_BG);
        g->setTextPadding(SCR_W);
        g->drawString("tap here for panic / all notes off", SCR_W / 2, 158, 1);
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
    g->fillRect(0, BAR_H + 1, SCR_W, BTN_Y - BAR_H - 1, C_BG);
    g->setTextDatum(MC_DATUM);
    g->setTextColor(C_WARN, C_BG);
    g->setTextPadding(SCR_W);
    g->drawString(toastMsg, SCR_W / 2, (BAR_H + BTN_Y) / 2, 4);
}

void uiMidiActivity() {
    midiBlinkUntil = millis() + 120;
}

void uiTick(const PatchState &s) {
    if (!g) return;
    // Repaint the main area the moment a toast expires.
    if (toastUntil && millis() >= toastUntil) {
        toastUntil = 0;
        g->fillRect(0, BAR_H + 1, SCR_W, BTN_Y - BAR_H - 1, C_BG);
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
    if (y > BAR_H) return BTN_PANIC;
    return BTN_NONE;
}
