#pragma once

// ---------------------------------------------------------------------------
// CYD (ESP32-2432S028R) hardware map
//
// Display pins live in platformio.ini as TFT_eSPI build flags. The XPT2046
// touch controller sits on a *second* SPI bus, which is why it is declared
// here rather than there.
// ---------------------------------------------------------------------------

#define PIN_TOUCH_CLK    25
#define PIN_TOUCH_MOSI   32
#define PIN_TOUCH_MISO   39
#define PIN_TOUCH_CS     33
#define PIN_TOUCH_IRQ    36

#define PIN_BACKLIGHT    21
#define BL_PWM_CHANNEL   0
#define BL_PWM_FREQ      5000
#define BL_PWM_BITS      8

// 1 = landscape, 3 = landscape rotated 180. The stock demo booted as
// LANDSCAPE_INVERTED, so flip to 3 if yours comes up upside down.
#define SCREEN_ROTATION  1

// ---------------------------------------------------------------------------
// Touch calibration
//
// Raw XPT2046 corner values. These vary panel to panel. Send "cal" over serial
// to enter calibration mode - it prints raw x/y for every touch so you can
// read off your own corners.
// ---------------------------------------------------------------------------
#define TS_RAW_MINX      200
#define TS_RAW_MAXX      3700
#define TS_RAW_MINY      240
#define TS_RAW_MAXY      3800

#define TOUCH_DEBOUNCE_MS   180
#define TOUCH_REPEAT_MS     420   // hold a bank/patch button to auto-repeat

// ---------------------------------------------------------------------------
// Link + power
// ---------------------------------------------------------------------------
#define SERIAL_BAUD         115200
#define FW_VERSION          "1.0.0"

// No state frame from the Pi for this long -> treat the link as down.
#define LINK_TIMEOUT_MS     6000UL

// Backlight levels (0-255) and idle dimming. This runs off the same battery as
// the Pi, so dimming is a real power saving, not a nicety.
#define BL_LEVEL_FULL       255
#define BL_LEVEL_DIM        30
#define BL_IDLE_MS          120000UL   // 0 disables idle dimming

#define HELLO_RETRY_MS      2000UL

// ---------------------------------------------------------------------------
// Touch axis orientation
//
// If touches land in the wrong place after you have set the raw ranges above,
// flip these rather than editing code. Work through them in order: swap first,
// then the inversions.
// ---------------------------------------------------------------------------
#define TOUCH_SWAP_XY    0
#define TOUCH_INVERT_X   0
#define TOUCH_INVERT_Y   0
