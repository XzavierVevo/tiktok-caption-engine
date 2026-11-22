"""Opt-in, fail-closed Spark UI driver. No fixed-position button taps.

Uses accessibility labels/state and bounds from a fresh UI hierarchy. Labels
are exact matches; unknown/ambiguous screens stop rather than guessing.
The legacy DuoPlus template remains the default until an idle-phone canary.
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET


class SparkUIError(RuntimeError):
    def __init__(self, message, *, stage):
        super().__init__(message)
        self.stage = stage


class SparkUI:
    def __init__(self, command, clipboard, valid_code, *, package, labels=None, wait_s=20):
        self.command = command
        self.clipboard = clipboard
        self.valid_code = valid_code
        self.package = package
        self.wait_s = wait_s
        self.stage = "open_post"
        self.labels = {
            "share": ["Share", "More options"],
            "settings": ["Ad settings"],
            "authorization": ["Ad authorization"],
            "generate": ["Generate code", "Generate"],
            "copy": ["Copy code", "Copy"],
            "save": ["Save"],
            **(labels or {}),
        }

    def snapshot(self):
        raw = self.command("uiautomator dump /sdcard/spark-ui.xml >/dev/null && cat /sdcard/spark-ui.xml")
        start = raw.find("<?xml")
        if start < 0:
            start = raw.find("<hierarchy")
        try:
            root = ET.fromstring(raw[start:]) if start >= 0 else None
        except ET.ParseError:
            root = None
        if root is None or not any(n.get("package") == self.package for n in root.iter("node")):
            raise SparkUIError("TikTok accessibility hierarchy unavailable", stage=self.stage)
        return root

    def matches(self, root, key):
        nodes = [n for n in root.iter("node")
                if n.get("package") == self.package
                and n.get("enabled", "true") == "true"
                and (n.get("text") in self.labels[key] or n.get("content-desc") in self.labels[key]
                     or (key == "share" and (n.get("resource-id") == self.package + ":id/v6f"
                         or re.match(r"^Share (video|post)\.", n.get("content-desc", ""))))) ]
        # Android exposes a labelled button and its text child as the same control.
        # Collapse that pair; two separate controls remain ambiguous.
        return [n for n in nodes if not any(n is not parent and n in list(parent.iter()) for parent in nodes)]

    def one(self, root, key):
        nodes = self.matches(root, key)
        if len(nodes) != 1:
            raise SparkUIError(f"Expected one {key} control; found {len(nodes)}", stage=self.stage)
        return nodes[0]

    def tap(self, node):
        bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
        if not bounds:
            raise SparkUIError("Control has no usable bounds", stage=self.stage)
        x1, y1, x2, y2 = map(int, bounds.groups())
        if x2 <= x1 or y2 <= y1:
            raise SparkUIError("Control is not visible", stage=self.stage)
        self.command(f"input tap {(x1+x2)//2} {(y1+y2)//2}")

    def wait(self, *keys):
        deadline = time.monotonic() + self.wait_s
        while True:
            root = self.snapshot()
            if any(self.matches(root, k) for k in keys):
                return root
            if time.monotonic() >= deadline:
                raise SparkUIError(f"Timed out waiting for {' / '.join(keys)}", stage=self.stage)
            time.sleep(1)

    def settings_menu(self):
        root = self.wait("settings", "share")
        if self.matches(root, "settings"):
            return root
        self.tap(self.one(root, "share"))
        time.sleep(1)
        for _ in range(5):
            root = self.snapshot()
            if self.matches(root, "settings"):
                return root
            rows = [n for n in root.iter("node") if n.get("package") == self.package
                    and n.get("resource-id") == self.package + ":id/a59" and n.get("scrollable") == "true"]
            if len(rows) != 1:
                raise SparkUIError("Post actions row is unavailable", stage=self.stage)
            bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", rows[0].get("bounds", ""))
            if not bounds:
                raise SparkUIError("Post actions row is not visible", stage=self.stage)
            a,b,c,d = map(int, bounds.groups())
            self.command(f"input swipe {c-(c-a)//10} {(b+d)//2} {a+(c-a)//10} {(b+d)//2} 400")
            time.sleep(1)
        raise SparkUIError("Ad settings was not found in post actions", stage=self.stage)

    def authorization_switch(self, root):
        label = self.one(root, "authorization")
        parents = {child: parent for parent in root.iter() for child in parent}
        scope = label
        while scope is not None:
            toggles = [n for n in scope.iter("node") if n.get("checkable") == "true"
                       and n.get("package") == self.package and n.get("enabled", "true") == "true"]
            if len(toggles) == 1:
                return toggles[0]
            if len(toggles) > 1:
                break
            scope = parents.get(scope)
        raise SparkUIError("Cannot identify authorization switch state unambiguously", stage=self.stage)

    def run(self, video_id):
        if not str(video_id).isdigit():
            raise ValueError("Numeric post ID required")
        baseline = self.clipboard().strip()
        self.command(f"am start -a android.intent.action.VIEW -d 'snssdk1233://aweme/detail/{video_id}' {self.package}")
        self.stage = "open_settings"
        root = self.settings_menu()
        self.tap(self.one(root, "settings"))
        self.stage = "authorization"
        root = self.wait("authorization")
        toggle = self.authorization_switch(root)
        if toggle.get("checked") == "false":
            self.tap(toggle)
            deadline = time.monotonic() + self.wait_s
            while True:
                root = self.snapshot()
                if self.authorization_switch(root).get("checked") == "true":
                    break
                if time.monotonic() >= deadline:
                    raise SparkUIError("Authorization did not become enabled", stage=self.stage)
                time.sleep(1)
        elif toggle.get("checked") != "true":
            raise SparkUIError("Authorization state is unknown", stage=self.stage)
        # Never toggle an already-enabled switch. Never click Delete/Revoke.
        if self.matches(root, "save"):
            self.tap(self.one(root, "save"))
        self.stage = "generate"
        root = self.wait("copy", "generate")
        if not self.matches(root, "copy"):
            self.tap(self.one(root, "generate"))
            root = self.wait("copy")
        self.stage = "copy"
        for _ in range(3):
            root = self.wait("copy")
            self.tap(self.one(root, "copy"))
            time.sleep(1)
            code = self.clipboard().strip()
            screen_codes = {n.get("text", "").strip() for n in root.iter("node")
                            if n.get("package") == self.package}
            if self.valid_code(code) and (code != baseline or code in screen_codes):
                self.stage = "complete"
                return code
        raise SparkUIError("Copy completed without a fresh, valid Spark code", stage=self.stage)
