#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyobjc-framework-Quartz", "pyobjc-framework-ApplicationServices",
#                 "pyobjc-framework-Cocoa", "pyobjc-framework-AVFoundation"]
# ///
"""amazing-grok for macOS: Option-key hotkey and a menu bar indicator.

Runs inside "Amazing Grok.app" (see build.sh), so macOS asks for Input
Monitoring, Accessibility and Microphone for that app instead of whatever
terminal you happen to use. Everything else — dictation, talking to Grok,
auth — is the regular amazing-grok script, which this loads as a module.

  tap Option            dictate; tap again to stop
  double tap Option     talk to Grok; tap once to hang up

A tap is a quick press and release of Option (either side) on its own: no
other key, modifier, click or scroll in between. Option+key shortcuts (such
as AeroSpace's alt-1) never trigger it. Two independent checks make sure:
  * the event tap sits at the HID level, ahead of global-hotkey dispatch, so
    it sees keys even when a window manager swallows them;
  * on release it asks the HID system whether a real key or click happened
    during the press.
"""
import importlib.machinery
import importlib.util
import os
import sys
import threading
import time
from pathlib import Path

import AppKit
import AVFoundation
import objc
import Quartz
from ApplicationServices import (AXIsProcessTrusted, AXIsProcessTrustedWithOptions,
                                 kAXTrustedCheckOptionPrompt)

SCRIPT = Path(__file__).resolve().parent.parent / "amazing-grok"
MAX_TAP_S = float(os.environ.get("AMAZING_GROK_MAX_TAP_MS", "350")) / 1000
OPTION_KEYS = {58: 0x20, 61: 0x40}  # left / right Option -> device flag bit
if os.environ.get("AMAZING_GROK_OPTION") == "right":
    OPTION_KEYS = {61: 0x40}
elif os.environ.get("AMAZING_GROK_OPTION") == "left":
    OPTION_KEYS = {58: 0x20}
DEBUG = "--debug" in sys.argv


def load_amazing_grok():
    loader = importlib.machinery.SourceFileLoader("amazing_grok", str(SCRIPT))
    spec = importlib.util.spec_from_loader("amazing_grok", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


ag = load_amazing_grok()


def say(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


# ── menu bar ──────────────────────────────────────────────────

class Indicator(AppKit.NSObject):
    """Redrawn 10x a second from amazing-grok's state file. Idle is a template
    SF Symbol, drawn like the built-in menu bar items."""

    def init(self):
        self = objc.super(Indicator, self).init()
        self.item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            AppKit.NSVariableStatusItemLength)
        menu = AppKit.NSMenu.alloc().init()
        self.info = menu.addItemWithTitle_action_keyEquivalent_("", None, "")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        menu.addItemWithTitle_action_keyEquivalent_("Quit Amazing Grok", "terminate:", "q").setTarget_(AppKit.NSApp)
        self.item.setMenu_(menu)
        self.font = AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(12, AppKit.NSFontWeightSemibold)
        self.setup = ""  # missing permissions, if any
        self.shown, self.state, self.since = None, None, time.monotonic()
        AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.1, self, "tick:", None, True)
        self.tick_(None)
        return self

    def tick_(self, _timer):
        state = "setup" if self.setup else ag.read_state()
        if state != self.state:
            self.state, self.since = state, time.monotonic()
        age = int(time.monotonic() - self.since)
        red, amber, blue = (AppKit.NSColor.systemRedColor(), AppKit.NSColor.systemOrangeColor(),
                            AppKit.NSColor.systemBlueColor())
        title = ""
        if state == "recording":
            symbol, color, info = "waveform", red, "Dictating — tap Option to stop"
            title = f" {age // 60}:{age % 60:02d}"
        elif state == "transcribing":
            symbol, color, info = "ellipsis", None, "Finishing the transcript…"
        elif state == "talking":
            symbol, color, info = "bubble.left.and.bubble.right.fill", blue, "Talking to Grok — tap Option to hang up"
            title = f" {age // 60}:{age % 60:02d}"
        elif state == "setup":
            symbol, color = "exclamationmark.triangle", amber
            info = f"Needs {self.setup} (System Settings › Privacy & Security)"
        elif state == "error":
            try:
                msg = ag.MSG.read_text().strip()
            except OSError:
                msg = ""
            symbol, color, info = "exclamationmark.triangle", red, f"Failed: {msg or 'see log'}"
        else:
            symbol, color, info = "waveform", None, "Tap Option: dictate · double tap: talk to Grok"
        key = (symbol, color, title, info)
        if key == self.shown:
            return
        self.shown = key
        img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, "Amazing Grok")
        conf = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(14, AppKit.NSFontWeightRegular)
        if color is not None:
            conf = conf.configurationByApplyingConfiguration_(
                AppKit.NSImageSymbolConfiguration.configurationWithPaletteColors_([color]))
        img = img.imageWithSymbolConfiguration_(conf)
        img.setTemplate_(color is None)
        button = self.item.button()
        button.setImage_(img)
        button.setImagePosition_(AppKit.NSImageLeft)
        attrs = {AppKit.NSFontAttributeName: self.font}
        if color is not None:
            attrs[AppKit.NSForegroundColorAttributeName] = color
        button.setAttributedTitle_(AppKit.NSAttributedString.alloc().initWithString_attributes_(title, attrs))
        self.info.setTitle_(info)


class Retry(AppKit.NSObject):
    """Calls fn on the main thread now and every second until it returns True."""

    def initWith_(self, fn):
        self = objc.super(Retry, self).init()
        self.fn = fn
        if not fn():
            AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1.0, self, "fire:", None, True)
        return self

    def fire_(self, timer):
        if self.fn():
            timer.invalidate()


# ── Option-tap hotkey ─────────────────────────────────────────

def hardware_input_within(seconds):
    """Did a real key, click or scroll happen in the last `seconds`? Asked of
    the HID system, so keys a window manager consumed still count."""
    hid = Quartz.kCGEventSourceStateHIDSystemState
    return any(Quartz.CGEventSourceSecondsSinceLastEventType(hid, t) < seconds for t in (
        Quartz.kCGEventKeyDown, Quartz.kCGEventLeftMouseDown, Quartz.kCGEventRightMouseDown,
        Quartz.kCGEventOtherMouseDown, Quartz.kCGEventScrollWheel))


def main():
    # Ask for everything at launch, so no prompt shows up mid-dictation.
    if not Quartz.CGPreflightListenEventAccess():
        Quartz.CGRequestListenEventAccess()
    AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
    AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
        AVFoundation.AVMediaTypeAudio, lambda granted: None)

    press = {"key": None, "clean": False, "t0": 0.0}
    others = (Quartz.kCGEventFlagMaskShift | Quartz.kCGEventFlagMaskControl |
              Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskSecondaryFn)

    def on_event(proxy, etype, event, refcon):
        if etype in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
            Quartz.CGEventTapEnable(tap[0], True)
            return event
        if etype != Quartz.kCGEventFlagsChanged:
            press["clean"] = False  # any key, click or scroll spoils a tap
            return event
        keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
        flags = Quartz.CGEventGetFlags(event)
        bit = OPTION_KEYS.get(keycode)
        if bit is None:
            press["clean"] = False  # another modifier joined in
            return event
        if flags & bit:
            press.update(key=keycode, clean=press["key"] is None and not flags & others, t0=time.monotonic())
        elif keycode == press["key"]:
            held = time.monotonic() - press["t0"]
            ok = press["clean"] and held < MAX_TAP_S and not hardware_input_within(held + 0.02)
            press.update(key=None, clean=False)
            if DEBUG:
                say(f"option released after {held:.3f}s, tap={ok}")
            if ok:  # tap() waits out the double-tap window, so keep it off the run loop
                threading.Thread(target=ag.cmd_tap, daemon=True).start()
        return event

    mask = 0
    for t in (Quartz.kCGEventFlagsChanged, Quartz.kCGEventKeyDown, Quartz.kCGEventLeftMouseDown,
              Quartz.kCGEventRightMouseDown, Quartz.kCGEventOtherMouseDown, Quartz.kCGEventScrollWheel):
        mask |= Quartz.CGEventMaskBit(t)
    tap = [None]

    def install_tap():
        # Without Input Monitoring a tap is created but never receives
        # anything, so wait for the grant rather than install a deaf one.
        missing = [name for name, ok in (("Input Monitoring", Quartz.CGPreflightListenEventAccess()),
                                         ("Accessibility", AXIsProcessTrusted())) if not ok]
        indicator.setup = " + ".join(missing)
        if missing:
            return False
        tap[0] = Quartz.CGEventTapCreate(Quartz.kCGHIDEventTap, Quartz.kCGHeadInsertEventTap,
                                         Quartz.kCGEventTapOptionListenOnly, mask, on_event, None)
        if tap[0] is None:
            indicator.setup = "an event tap (refused)"
            return False
        src = Quartz.CFMachPortCreateRunLoopSource(None, tap[0], 0)
        Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), src, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap[0], True)
        say("ready: tap Option to dictate, double tap to talk to Grok")
        return True

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)  # no Dock icon, never takes focus
    indicator = Indicator.alloc().init()
    retry = Retry.alloc().initWith_(install_tap)  # noqa: F841 (kept alive by the run loop)
    app.run()


if __name__ == "__main__":
    main()
