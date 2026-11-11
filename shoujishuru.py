# -*- coding: utf-8 -*-
import base64
import ctypes
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid

import pyautogui
import pyperclip
from flask import Flask, jsonify, request, send_file

if os.name == "nt":
    import ctypes.wintypes as wintypes

    user32 = ctypes.WinDLL("user32", use_errno=True)
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    MOUSEEVENTF_WHEEL = 0x0800
    WHEEL_DELTA = 120

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
        ]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]

    INPUT_MOUSE = 0
    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    VK_LBUTTON = 0x01
    VK_RBUTTON = 0x02
    KEYSTATE_PRESSED = 0x8000

    CLICK_HOLD_SECONDS = 0.03

    def sendMouseEvent(flags, quiet=False):
        event = _INPUT()
        event.type = INPUT_MOUSE
        event.union.mi = _MOUSEINPUT(0, 0, 0, flags, 0, None)
        result = user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_INPUT))
        if result == 0 and not quiet:
            err = ctypes.get_last_error()
            print("鼠标：SendInput 被系统拦截（错误码 %d），尝试 mouse_event 兜底" % err, flush=True)
            user32.mouse_event(flags, 0, 0, 0, 0)

    def sendSingleInput(flags):
        event = _INPUT()
        event.type = INPUT_MOUSE
        event.union.mi = _MOUSEINPUT(0, 0, 0, flags, 0, None)
        return user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_INPUT))

    def sendMouseEventPair(down, up):
        # 按下、抬起拆成两次 SendInput 提交，中间保留一段真实的按压时长。
        # 放在同一批里会被系统当成 0ms 的异常点击（部分程序直接忽略），
        # 一旦抬起事件被拦截就会卡键，被目标程序识别成“长按/拖拽”。
        vk = VK_LBUTTON if down == MOUSEEVENTF_LEFTDOWN else VK_RBUTTON
        injected = sendSingleInput(down) == 1
        if not injected:
            err = ctypes.get_last_error()
            user32.mouse_event(down, 0, 0, 0, 0)
        time.sleep(CLICK_HOLD_SECONDS)
        downConfirmed = bool(user32.GetAsyncKeyState(vk) & KEYSTATE_PRESSED)
        try:
            if sendSingleInput(up) != 1:
                user32.mouse_event(up, 0, 0, 0, 0)
        finally:
            # 任何情况下都要确认已经抬起，杜绝卡键被当成“长按”
            time.sleep(0.01)
            if user32.GetAsyncKeyState(vk) & KEYSTATE_PRESSED:
                user32.mouse_event(up, 0, 0, 0, 0)
                time.sleep(0.01)
        if user32.GetAsyncKeyState(vk) & KEYSTATE_PRESSED:
            raise RuntimeError("鼠标抬起被拦截，可能卡键——请物理点一下鼠标解除")
        if not injected:
            if not downConfirmed:
                raise RuntimeError(
                    "鼠标注入通道被系统拦截（SendInput err=%d，mouse_event 兜底也无效）——"
                    "请退出华为电脑管家/杀毒软件/远控软件后重试，或重启电脑" % err
                )
            print("鼠标：SendInput 被拦截（err=%d），mouse_event 兜底成功（回执确认）" % err, flush=True)

    def sendScroll(clicks):
        # pyautogui.scroll 把格数直接当 wheel delta 传（没乘 120），
        # 传 12 格实际只滚 1/120 格——"怎么滑都只滚一点点"的根因。
        # 这里自实现：1 格 = 120 wheel delta（Windows 标准 WHEEL_DELTA）
        event = _INPUT()
        event.type = INPUT_MOUSE
        event.union.mi = _MOUSEINPUT(0, 0, clicks * WHEEL_DELTA, MOUSEEVENTF_WHEEL, 0, None)
        result = user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_INPUT))
        if result == 0:
            err = ctypes.get_last_error()
            print("鼠标：SendInput 滚轮被拦（err=%d），mouse_event 兜底" % err, flush=True)
            user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, clicks * WHEEL_DELTA, 0)

    # Shell.Application 的 COM 直调版 ToggleDesktop（Win+D 的底层实现）：
    # 进程内加载 shell32 直接调 API，无需另起 PowerShell，毫秒级返回。
    # 失败时调用方退回 PowerShell 路径兜底。
    CLSID_SHELL_APPLICATION = "{13709620-C279-11CE-A49E-444553540000}"
    IID_IDISPATCH = "{00020400-0000-0000-C000-000000000046}"
    IID_NULL = "{00000000-0000-0000-0000-000000000000}"
    CLSCTX_SERVER = 0x5
    DISPATCH_METHOD = 0x1
    LOCALE_USER_DEFAULT = 0x0400

    class _COMGUID(ctypes.Structure):
        _fields_ = [
            ("data1", wintypes.DWORD),
            ("data2", wintypes.WORD),
            ("data3", wintypes.WORD),
            ("data4", ctypes.c_ubyte * 8),
        ]

    class _DISPPARAMS(ctypes.Structure):
        _fields_ = [
            ("rgvarg", ctypes.c_void_p),
            ("rgdispidNamedArgs", ctypes.c_void_p),
            ("cArgs", wintypes.DWORD),
            ("cNamedArgs", wintypes.DWORD),
        ]

    def _comGuid(text):
        u = uuid.UUID(text)
        g = _COMGUID()
        g.data1 = u.time_low
        g.data2 = u.time_mid
        g.data3 = u.time_hi_version
        g.data4 = (ctypes.c_ubyte * 8)(*u.bytes[8:])
        return g

    def toggleDesktopViaCom():
        # 返回 True=已切换；False=失败（调用方走 PowerShell 兜底）
        ole32 = ctypes.WinDLL("ole32", use_errno=True)
        initialized = ole32.CoInitialize(None) >= 0
        try:
            clsid = _comGuid(CLSID_SHELL_APPLICATION)
            iidDispatch = _comGuid(IID_IDISPATCH)
            shellDisp = ctypes.c_void_p()
            hr = ole32.CoCreateInstance(
                ctypes.byref(clsid), None, CLSCTX_SERVER,
                ctypes.byref(iidDispatch), ctypes.byref(shellDisp))
            if hr < 0 or not shellDisp.value:
                return False
            try:
                # COM 对象布局: 对象首字段=vtable 指针
                objPtr = ctypes.cast(shellDisp.value, ctypes.POINTER(ctypes.c_void_p))
                vtbl = ctypes.cast(objPtr[0], ctypes.POINTER(ctypes.c_void_p))
                # vtable: 0-2=IUnknown(QueryInterface/AddRef/Release), 3=GetTypeInfoCount,
                # 4=GetTypeInfo, 5=GetIDsOfNames, 6=Invoke
                getIds = ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_COMGUID),
                    ctypes.POINTER(ctypes.c_wchar_p), ctypes.c_uint,
                    wintypes.DWORD, ctypes.POINTER(ctypes.c_long))(vtbl[5])
                invoke = ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.c_long,
                    ctypes.POINTER(_COMGUID), wintypes.DWORD, wintypes.WORD,
                    ctypes.POINTER(_DISPPARAMS), ctypes.c_void_p,
                    ctypes.c_void_p, ctypes.c_void_p)(vtbl[6])
                release = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtbl[2])
                name = ctypes.c_wchar_p("ToggleDesktop")
                dispid = ctypes.c_long(-1)
                iidNull = _comGuid(IID_NULL)
                hr = getIds(shellDisp, ctypes.byref(iidNull), ctypes.byref(name),
                            1, LOCALE_USER_DEFAULT, ctypes.byref(dispid))
                if hr < 0:
                    return False
                params = _DISPPARAMS(None, None, 0, 0)
                hr = invoke(shellDisp, dispid.value, ctypes.byref(iidNull),
                            LOCALE_USER_DEFAULT, DISPATCH_METHOD,
                            ctypes.byref(params), None, None, None)
                return hr >= 0
            finally:
                release(shellDisp)
        finally:
            if initialized:
                ole32.CoUninitialize()

    def startMenuShown():
        buf = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(user32.GetForegroundWindow(), buf, 128)
        return "CoreWindow" in buf.value or "XamlExplorer" in buf.value

    def startMenuShownSoon():
        # 弹出有动画/焦点切换延迟，单击后 0.45s 单次检测可能测不到——轮询最多 1.5 秒
        for _ in range(5):
            if startMenuShown():
                return True
            time.sleep(0.3)
        return False

    # Win 短点打开开始菜单（remote-touchpad 同款通路）：ctypes SendInput 直发合成
    # Win 键（VK_LWIN down→up），全程无 shell 子进程——360 对 powershell.exe 报
    # Trojan.Generic 拦截，UIA 定位/代点路线废弃；exe 加入信任区后毫秒级弹出
    # （Go 版同款 SendInput 在被信任进程实测有效）。失败备选 pyautogui.press。

    def sendKeySequence(keys):
        # ctypes SendInput 直发键序列：down→(真实节奏)→up 分批提交，
        # 返回 True=全部事件送达；False=通道被拦（调用方走 pyautogui 备选）
        VK = {
            "winleft": 0x5B, "winright": 0x5C, "ctrl": 0x11, "shift": 0x10,
            "alt": 0x12, "enter": 0x0D, "esc": 0x1B, "tab": 0x09,
            "space": 0x20, "backspace": 0x08, "delete": 0x2E,
            "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
            "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
            "insert": 0x2D,
        }
        vks = []
        for k in keys:
            vk = VK.get(k)
            if vk is None:
                if len(k) == 1:
                    vk = ord(k.upper())
                else:
                    return False
            vks.append(vk)
        for vk in vks:
            e = _INPUT()
            e.type = INPUT_KEYBOARD
            e.union.ki = _KEYBDINPUT(vk, 0, 0, 0, None)
            if user32.SendInput(1, ctypes.byref(e), ctypes.sizeof(_INPUT)) != 1:
                return False
        time.sleep(0.05)
        for vk in reversed(vks):
            e = _INPUT()
            e.type = INPUT_KEYBOARD
            e.union.ki = _KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, None)
            if user32.SendInput(1, ctypes.byref(e), ctypes.sizeof(_INPUT)) != 1:
                return False
        return True

    def openStartMenu():
        # ①ctypes SendInput 直发 Win 键（exe 加入信任区后毫秒级，Go 版同款实测有效）
        if sendKeySequence(["winleft"]):
            if startMenuShownSoon():
                return True
            print("键盘：Win 键已送达但开始菜单未弹出", flush=True)
        else:
            print("键盘：SendInput Win 键被拦，转 pyautogui 备选", flush=True)
        # ②pyautogui 备选（keybd_event 通道）
        try:
            pyautogui.press("winleft")
            return startMenuShownSoon()
        except Exception as exc:
            print("pyautogui Win 键失败：%r" % exc, flush=True)
            return False
else:
    user32 = None
    sendMouseEvent = None

HOST = "0.0.0.0"
PORT = 8765
MAX_TEXT_LENGTH = 10000
CLIPBOARD_DELAY_SECONDS = 0.15
ENTER_DELAY_SECONDS = 0.25
IMAGE_PASTE_DELAY_SECONDS = 0.4
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff")
# 打包成 exe 后 __file__ 指向 _internal，收件箱必须建在 exe 同级目录
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ATTACH_DIR = os.path.join(BASE_DIR, "收件箱")
MAX_UPLOAD_MB = 200

pasteLock = threading.Lock()
mouseLock = threading.Lock()
kbLock = threading.Lock()  # 键盘按键注入与粘贴序列互斥，防 Ctrl+V 组合被打断交错

pyautogui.PAUSE = 0
pyautogui.FAILSAFE = False

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

@app.after_request
def addNoCacheHeaders(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

pageHtml = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>文字投递</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { min-height: 100%; }
body {
  background: rgb(245, 247, 252);
  color: rgb(50, 60, 80);
  font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
  display: flex;
  flex-direction: column;
  padding: 12px 20px 24px;
}

.inputShell {
  position: relative;
  border: 1px solid rgb(185, 198, 225);
  border-radius: 12px;
  background: rgb(255, 255, 255);
  box-shadow: 0 2px 10px rgba(180, 195, 225, 0.35);
  height: calc(36vh + 54px);
  display: flex;
  flex-direction: column;
  overflow: visible;
}
.inputShell:focus-within { border-color: rgb(80, 130, 235); }
.inputShell::before {
  content: "";
  position: absolute;
  top: 0;
  right: 0;
  width: 39px;
  height: 46px;
  background: rgb(224, 238, 255);
  border-bottom: 1px solid rgb(185, 198, 225);
  border-radius: 0 11px 0 0;
  pointer-events: none;
  z-index: 1;
}
.inputShell::after {
  content: "";
  position: absolute;
  top: 46px;
  right: 0;
  width: 39px;
  bottom: 47px;
  background: rgb(240, 244, 252);
  pointer-events: none;
  z-index: 0;
}
.sideToggle {
  position: absolute;
  top: 50%;
  right: 6px;
  width: 27px;
  transform: translateY(-50%);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  padding: 6px 0;
  z-index: 2;
  background: rgb(224, 238, 255);
  border: 1px solid rgb(110, 155, 235);
  border-radius: 6px;
}
.sideToggle input {
  width: 14px;
  height: 14px;
  margin: 0;
  flex-shrink: 0;
  appearance: none;
  -webkit-appearance: none;
  background: rgb(224, 238, 255);
  border: 1px solid rgb(110, 155, 235);
  border-radius: 3px;
  position: relative;
  cursor: pointer;
}
.sideToggle input:checked {
  background: rgb(64, 110, 255);
  border-color: rgb(64, 110, 255);
}
.sideToggle input:checked::before {
  content: "";
  position: absolute;
  left: 2px;
  top: 0px;
  width: 5px;
  height: 8px;
  border: solid rgb(255, 255, 255);
  border-width: 0 2px 2px 0;
  transform: rotate(45deg);
}
.sideToggle span {
  writing-mode: vertical-rl;
  text-orientation: mixed;
  font-size: 12px;
  color: rgb(70, 85, 115);
  line-height: 1;
  letter-spacing: 1px;
}
.rightDivider {
  position: absolute;
  right: 39px;
  top: 0;
  bottom: 47px;
  width: 1px;
  background: rgb(185, 198, 225);
  pointer-events: none;
}
#pcBell {
  position: absolute;
  top: -7px;
  left: 7px;
  width: 30px;
  height: 30px;
  padding: 0;
  box-sizing: border-box;
  border-radius: 50%;
  border: 1px solid rgb(120, 150, 205);
  background-color: rgb(255, 255, 255);
  color: rgb(25, 31, 50);
  font-family: "微软雅黑", sans-serif;
  font-size: 14px;
  font-weight: bold;
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 6;
  cursor: pointer;
  box-shadow: 0 2px 6px rgba(180, 195, 225, 0.45);
  -webkit-appearance: none;
  appearance: none;
  outline: none;
}
#pcBell:active {
  background-color: rgb(220, 240, 220);
}
#pcBell.hasUnread::after {
  content: "";
  position: absolute;
  top: -2px;
  right: -2px;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background-color: rgb(230, 80, 80);
  border: 1px solid rgb(255, 255, 255);
}
#pcInbox {
  display: none;
  flex-direction: column;
  margin: 6px 46px 1px 7px;
  padding: 2px 10px;
  max-height: 120px;
  overflow-y: auto;
  background-color: rgb(255, 255, 255);
  border: 1px solid rgb(185, 198, 225);
  border-radius: 12px;
  flex-shrink: 0;
  transform: translateY(1px);
}
#pcInbox.open {
  display: flex;
}
#pcInbox::-webkit-scrollbar {
  width: 6px;
}
#pcInbox::-webkit-scrollbar-track {
  background: transparent;
  margin: 6px 0;
}
#pcInbox::-webkit-scrollbar-thumb {
  background: rgb(185, 198, 225);
  border-radius: 3px;
}
.pcChip {
  -webkit-tap-highlight-color: transparent;
  padding: 2px 0;
  border-bottom: 1px solid rgb(185, 198, 225);
  font-size: 14px;
  line-height: 1.4;
  color: rgb(40, 50, 70);
  word-break: break-all;
  cursor: pointer;
  text-align: left;
}
.pcChip:last-child {
  border-bottom: none;
}
.pcChip:active {
  background-color: rgb(240, 243, 248);
}
.pcChipTime {
  font-size: 12px;
  color: rgb(140, 150, 175);
  margin-bottom: 0;
  text-align: right;
}
textarea {
  flex: 1;
  min-width: 0;
  min-height: 0;
  resize: none;
  background: transparent;
  border: none;
  border-radius: 12px;
  color: rgb(40, 50, 70);
  font-size: 17px;
  line-height: 1.5;
  padding: 9px 14px 12px;
  text-indent: 26px;
  outline: none;
  -webkit-appearance: none;
  appearance: none;
  margin-right: 39px;
}
button {
  -webkit-tap-highlight-color: transparent;
  width: 100%;
  margin-top: 14px;
  padding: 14px 0;
  font-size: 17px;
  font-weight: 600;
  color: rgb(30, 80, 190);
  background: rgb(224, 238, 255);
  border: 1px solid rgb(110, 155, 235);
  border-radius: 12px;
}
button:active { background: rgb(198, 221, 255); }
button:disabled {
  background: rgb(232, 236, 244);
  border-color: rgb(200, 210, 225);
  color: rgb(150, 160, 180);
}
.sendRow {
  display: flex;
  padding: 0;
  border-top: 1px solid rgb(185, 198, 225);
  flex-shrink: 0;
}
#sendBtn, #pcSendBtn {
  flex: 1;
  margin-top: 0;
  height: 46px;
  padding: 10px 0 0;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  position: relative;
  border: none;
  border-right: 1px solid rgb(185, 198, 225);
  border-radius: 0;
}
.btnSub {
  position: absolute;
  bottom: 2px;
  left: 0;
  right: 0;
  text-align: center;
  font-size: 11px;
  color: rgb(150, 160, 180);
  line-height: 1;
  pointer-events: none;
}
#attachBtn {
  width: 39px;
  margin-top: 0;
  height: 46px;
  padding: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;
  font-weight: 400;
  line-height: 1;
  border: none;
  border-radius: 0;
}
#pcSendBtn { border-radius: 0 0 0 11px; }
#attachBtn { border-radius: 0 0 11px 0; }
#attachBar {
  display: none;
  flex-wrap: wrap;
  gap: 6px;
  padding: 10px 10px 0 10px;
  max-height: 96px;
  overflow-y: auto;
  flex-shrink: 0;
  margin-right: 39px;
}
.fileChip {
  position: relative;
  display: inline-flex;
  align-items: center;
  max-width: 100%;
  background: rgb(255, 255, 255);
  border: 1px solid rgb(185, 198, 225);
  border-radius: 8px;
  padding: 4px 10px 4px 8px;
  font-size: 12px;
  color: rgb(70, 85, 115);
}
.fileName {
  max-width: 110px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.fileDel {
  position: absolute;
  top: -6px;
  right: -6px;
  width: 16px;
  height: 16px;
  margin-top: 0;
  padding: 0;
  border: 1px solid rgb(185, 198, 225);
  border-radius: 50%;
  background: rgb(255, 255, 255);
  color: rgb(205, 75, 75);
  font-size: 11px;
  font-weight: 400;
  line-height: 1;
}
.fileDel:active { background: rgb(255, 235, 235); }
#attachSheetMask {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(20, 30, 50, 0.25);
  opacity: 0;
  visibility: hidden;
  transition: opacity 0.25s ease, visibility 0.25s ease;
  z-index: 70;
}
#attachSheetMask.open { opacity: 1; visibility: visible; }
#attachSheet {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgb(255, 255, 255);
  border-radius: 16px 16px 0 0;
  padding: 10px 16px 20px;
  transform: translateY(100%);
  visibility: hidden;
  transition: transform 0.25s ease, visibility 0.25s ease;
  z-index: 71;
}
#attachSheet.open { transform: translateY(0); visibility: visible; }
.sheetTitle {
  text-align: center;
  font-size: 13px;
  color: rgb(150, 160, 180);
  padding: 6px 0 8px;
  border-bottom: 1px solid rgb(232, 236, 244);
}
.sheetItem {
  width: 100%;
  margin-top: 0;
  padding: 14px 0;
  border: none;
  border-radius: 0;
  background: transparent;
  font-size: 16px;
  font-weight: 400;
  color: rgb(40, 50, 70);
  border-bottom: 1px solid rgb(232, 236, 244);
}
.sheetItem:active { background: rgba(120, 160, 235, 0.12); }
.sheetCancel {
  width: 100%;
  margin-top: 0;
  padding: 14px 0;
  border: none;
  border-radius: 0;
  background: transparent;
  font-size: 16px;
  font-weight: 400;
  color: rgb(150, 160, 180);
}
.sheetCancel:active { background: rgba(120, 160, 235, 0.12); }
#mousePad {
  margin-top: 4px;

  height: calc(36vh + 54px);
  background: rgb(255, 255, 255);
  border: 1px solid rgb(185, 198, 225);
  border-radius: 12px;
  box-shadow: 0 2px 10px rgba(180, 195, 225, 0.35);
  display: flex;
  overflow: visible;
}
.padMain {
  flex: 1;
  min-width: 0;
  position: relative;
  display: flex;
  flex-direction: column;
}
#touchpad {
  flex: 1;
  min-height: 0;
  position: relative;
  touch-action: none;
  user-select: none;
  -webkit-user-select: none;
  display: flex;
  align-items: center;
  justify-content: center;
}
#kbBtn {
  position: absolute;
  top: -7px;
  left: 7px;
  width: 30px;
  height: 30px;
  padding: 0;
  box-sizing: border-box;
  border-radius: 50%;
  border: 1px solid rgb(120, 150, 205);
  background-color: rgb(255, 255, 255);
  color: rgb(40, 50, 70);
  font-family: "微软雅黑", sans-serif;
  font-size: 13px;
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 5;
  cursor: pointer;
  box-shadow: 0 2px 6px rgba(180, 195, 225, 0.45);
  -webkit-appearance: none;
  appearance: none;
  outline: none;
  -webkit-touch-callout: none;
}
#kbBtn:active { background-color: rgb(220, 235, 255); }
#touchHint {
  font-size: 12px;
  color: rgb(160, 170, 190);
  text-align: center;
  line-height: 1.7;
  padding: 0 16px;
  pointer-events: none;
}
#scrollBar {
  width: 40px;
  border-left: 1px solid rgb(185, 198, 225);
  border-radius: 0 11px 11px 0;
  background: rgb(240, 244, 252);
  touch-action: none;
  display: flex;
  flex-direction: column;
}
.scrollArrow {
  height: 46px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  color: rgb(30, 80, 190);
  background: rgb(224, 238, 255);
}
.scrollArrow:active { background: rgb(198, 221, 255); }
#scrollUp { border-radius: 0 11px 0 0; }
#scrollDown { border-radius: 0 0 11px 0; }
#scrollTrack {
  flex: 1;
  position: relative;
  touch-action: none;
  border-top: 1px solid rgb(185, 198, 225);
  border-bottom: 1px solid rgb(185, 198, 225);
}
#scrollThumb {
  position: absolute;
  left: 6px;
  right: 6px;
  top: 50%;
  transform: translateY(-50%);
  height: 112px;
  border-radius: 6px;
  background: repeating-linear-gradient(
    to bottom,
    rgb(224, 238, 255) 0px,
    rgb(224, 238, 255) 5px,
    rgb(140, 175, 240) 5px,
    rgb(140, 175, 240) 7px
  );
  border: 1px solid rgb(110, 155, 235);
  pointer-events: none;
}
.btnRow {
  display: flex;
  border-top: 1px solid rgb(185, 198, 225);
}
.mouseBtn {
  flex: 1;
  margin-top: 0;
  height: 46px;
  padding: 0;
  border: none;
  border-radius: 0;
  background: rgb(224, 238, 255);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 15px;
  font-weight: 600;
  color: rgb(30, 80, 190);
}
.mouseBtn:active { background: rgb(198, 221, 255); }
#btnLeft { border-right: 1px solid rgb(185, 198, 225); border-radius: 0 0 0 11px; }

#status {
  position: absolute;
  bottom: 47px;
  left: 0;
  right: 0;
  text-align: center;
  font-size: 13px;
  color: rgb(135, 148, 172);
  pointer-events: none;
  z-index: 5;
}
#status.ok { color: rgb(35, 150, 95); }
#status.err { color: rgb(205, 75, 75); }
#status:empty { display: none; }
#menuBtn {
  position: absolute;
  top: 0;
  right: 0;
  width: 39px;
  height: 46px;
  margin-top: 0;
  padding: 0;
  border: none;
  border-radius: 0 11px 0 0;
  background: transparent;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  gap: 4px;
  z-index: 10;
}
#menuBtn:active { background: rgba(120, 160, 235, 0.18); }
#menuBtn .bar {
  width: 18px;
  height: 2px;
  border-radius: 2px;
  background: rgb(40, 70, 140);
}
#menuBtn.ok .bar { background: rgb(52, 199, 89); }
#menuBtn.err .bar { background: rgb(255, 69, 58); }
#panelMask {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(20, 30, 50, 0.25);
  opacity: 0;
  visibility: hidden;
  transition: opacity 0.25s ease, visibility 0.25s ease;
  z-index: 40;
}
#panelMask.open { opacity: 1; visibility: visible; }
#panel {
  position: absolute;
  top: 0;
  right: 40px;
  bottom: 47px;
  width: 33.33vw;
  min-width: 132px;
  background: rgb(240, 244, 252);
  border-left: 1px solid rgb(185, 198, 225);
  border-bottom: 1px solid rgb(185, 198, 225);
  box-shadow: -4px 0 16px rgba(120, 140, 180, 0.25);
  clip-path: inset(0 0 0 100%);
  transition: clip-path 0.25s ease, visibility 0s linear 0.25s;
  visibility: hidden;
  z-index: 50;
  padding: 10px 12px;
}
#panel.open {
  clip-path: inset(0 0 0 0);
  visibility: visible;
  transition: clip-path 0.25s ease, visibility 0s linear 0s;
}
.panelHead {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 4px;
}
.panelTitle {
  font-size: 16px;
  font-weight: 600;
  color: rgb(40, 70, 140);
}
#panelClose {
  width: 28px;
  height: 28px;
  margin-top: 0;
  padding: 0;
  border: none;
  border-radius: 8px;
  background: transparent;
  font-size: 16px;
  line-height: 1;
  color: rgb(120, 135, 165);
}
#panelClose:active { background: rgba(120, 160, 235, 0.18); }
.panelItem {
  font-size: 13px;
  color: rgb(70, 85, 115);
  padding: 5px 4px;
  border-bottom: 1px solid rgb(232, 236, 244);
}
.panelItem span { color: rgb(40, 70, 140); font-weight: 500; }
.panelToggle {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 5px 4px;
  border-bottom: 1px solid rgb(232, 236, 244);
}
.panelToggle input {
  width: 16px;
  height: 16px;
  margin: 0;
  accent-color: rgb(64, 110, 255);
}
.panelNote {
  margin-top: 6px;
  font-size: 12px;
  color: rgb(160, 170, 190);
}
.panelNav {
  width: 100%;
  margin-top: 0;
  padding: 5px 4px;
  border: none;
  border-radius: 0;
  background: transparent;
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 13px;
  font-weight: 400;
  color: rgb(70, 85, 115);
  border-bottom: 1px solid rgb(232, 236, 244);
}
.panelNav:active { background: rgba(120, 160, 235, 0.12); }
#inboxView {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgb(245, 247, 252);
  z-index: 60;
  padding: 12px 20px 24px;
  overflow-y: auto;
}
.inboxHead {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin: 0 0 14px;
}
#inboxClose {
  width: 28px;
  height: 28px;
  margin-top: 0;
  padding: 0;
  border: none;
  border-radius: 8px;
  background: transparent;
  font-size: 16px;
  line-height: 1;
  color: rgb(120, 135, 165);
}
#inboxClose:active { background: rgba(120, 160, 235, 0.18); }
.inboxRow {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  background: rgb(255, 255, 255);
  border: 1px solid rgb(185, 198, 225);
  border-radius: 10px;
  padding: 10px 12px;
  margin-bottom: 8px;
}
.inboxName {
  flex: 1;
  min-width: 0;
}
.inboxFileName {
  font-size: 14px;
  color: rgb(40, 50, 70);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.inboxMeta {
  font-size: 11px;
  color: rgb(150, 160, 180);
  margin-top: 2px;
}
.fileDl {
  flex-shrink: 0;
  padding: 8px 14px;
  border: 1px solid rgb(110, 155, 235);
  border-radius: 10px;
  background: rgb(224, 238, 255);
  color: rgb(30, 80, 190);
  font-size: 13px;
  text-decoration: none;
}
.fileDl:active { background: rgb(198, 221, 255); }
.inboxThumb {
  flex-shrink: 0;
  width: 48px;
  height: 48px;
  border-radius: 8px;
  object-fit: cover;
  border: 1px solid rgb(200, 210, 228);
  background: rgb(240, 244, 250);
  cursor: pointer;
}
#imgViewer {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.92);
  z-index: 90;
  display: flex;
  align-items: center;
  justify-content: center;
}
#imgViewer[hidden] { display: none; }
#imgViewerImg {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
}
#imgViewerClose {
  position: absolute;
  top: 16px;
  right: 16px;
  width: 36px;
  height: 36px;
  border: none;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.15);
  color: rgb(255, 255, 255);
  font-size: 22px;
  line-height: 1;
}
#imgViewerClose:active { background: rgba(255, 255, 255, 0.28); }
#inboxEmpty {
  text-align: center;
  color: rgb(160, 170, 190);
  font-size: 13px;
  margin-top: 40px;
}
#inboxErr {
  text-align: center;
  color: rgb(205, 75, 75);
  font-size: 13px;
  margin-top: 20px;
}
#kbMask {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(20, 30, 50, 0.25);
  opacity: 0;
  visibility: hidden;
  transition: opacity 0.25s ease, visibility 0.25s ease;
  z-index: 62;
}
#kbMask.open { opacity: 1; visibility: visible; }
#kbPanel {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgb(244, 247, 253);
  border-top: 1px solid rgb(185, 198, 225);
  box-shadow: 0 -2px 10px rgba(180, 195, 225, 0.35);
  transform: translateY(105%);
  transition: transform 0.25s ease;
  z-index: 63;
  padding: 8px 6px calc(6px + env(safe-area-inset-bottom, 0px));
  user-select: none;
  -webkit-user-select: none;
  -webkit-touch-callout: none;
  overflow: hidden;
}
#kbPanel.open { transform: translateY(0); }
#kbFuncRow {
  overflow: hidden;
  margin-bottom: 5px;
}
#kbFuncPages {
  display: flex;
  width: 200%;
  transition: transform 0.25s ease;
  touch-action: none;
}
.kbFuncPage {
  width: 50%;
  flex-shrink: 0;
  display: flex;
  gap: 4px;
}
#kbPages {
  display: flex;
  width: 200%;
  transition: transform 0.25s ease;
  touch-action: none;
}
.kbPage {
  width: 50%;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 0 2px;
}
.kbRow { display: flex; gap: 4px; }
.kbKey {
  flex: 1;
  height: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgb(255, 255, 255);
  border: 1px solid rgb(185, 198, 225);
  border-radius: 4px;
  font-size: 14px;
  color: rgb(40, 50, 70);
  cursor: pointer;
  -webkit-tap-highlight-color: transparent;
}
.kbKey.press { background: rgb(198, 221, 255); }
.kbKey.on {
  background: rgb(196, 222, 254);
  border-color: rgb(110, 155, 235);
  color: rgb(30, 80, 190);
  font-weight: 600;
}
.kbKey.wide { flex: 2.2; }
.kbKey.small { font-size: 11px; }
#kbDots {
  display: flex;
  justify-content: center;
  gap: 8px;
  padding: 8px 0 2px;
}
#kbDots span {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: rgb(185, 198, 225);
}
#kbDots span.on { background: rgb(80, 130, 235); }
#kbSettings { display: none; }
#kbPanel.setting #kbWrap { display: none; }
#kbPanel.setting #kbSettings { display: block; }
.kbSetHead {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 2px 10px;
  font-size: 15px;
  font-weight: 600;
  color: rgb(40, 70, 140);
}
#kbSetBack {
  width: auto;
  padding: 4px 10px;
  margin-top: 0;
  border: 1px solid rgb(185, 198, 225);
  border-radius: 6px;
  background: rgb(255, 255, 255);
  font-size: 13px;
  color: rgb(40, 70, 140);
}
.kbSetItem {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 4px;
  border-bottom: 1px solid rgb(185, 198, 225);
  font-size: 14px;
  color: rgb(40, 50, 70);
}
.kbSetItem:last-child { border-bottom: none; }
.kbSetItem input {
  width: 20px;
  height: 20px;
  margin: 0;
  flex-shrink: 0;
}
/* ---------- 横屏：左右对半（左投递右触控板），键盘两页同屏 ---------- */
@media (orientation: landscape) {
  body {
    height: 100vh;
    height: 100svh;
    display: grid;
    grid-template-columns: 1fr 1fr;
    grid-template-rows: 1fr auto;
    column-gap: 8px;
    padding: 6px 14px 8px;
    overflow: hidden;
  }
  .inputShell {
    grid-column: 1;
    grid-row: 1;
    height: auto;
  }

  #mousePad {
    grid-column: 2;
    grid-row: 1 / 3;
    height: auto;
    margin-top: 0;
  }
  #kbPages, #kbFuncPages {
    width: 100% !important;
    transform: none !important;
  }
  .kbPage, .kbFuncPage { width: 50%; }
  .kbKey { height: 38px; }
  #kbDots { display: none; }
  #kbPanel {
    padding: 6px 8px calc(6px + env(safe-area-inset-bottom, 0px));
  }
  /* 横屏：三横按钮与抽屉面板均锚定在输入卡右侧窄条区域 */
}
</style>
</head>
<body>
<div id="panelMask"></div>
<div id="inboxView" hidden>
  <div class="inboxHead">
    <span class="panelTitle">电脑收件箱</span>
    <button id="inboxClose">×</button>
  </div>
  <div id="inboxList"></div>
  <div id="inboxEmpty" hidden>收件箱还是空的</div>
  <div id="inboxErr" hidden>加载失败，请检查连接</div>
</div>
<div id="imgViewer" hidden>
  <img id="imgViewerImg" alt="">
  <button id="imgViewerClose">×</button>
</div>

<div class="inputShell">
  <button id="pcBell" aria-label="来自电脑的消息"><svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d="M12 3.5c-5 0-9 3.2-9 7.2 0 2.2 1.2 4.2 3.1 5.5-.1 1-.5 2.3-1.6 3.3 1.8-.2 3.3-.9 4.3-1.6 1 .3 2.1.4 3.2.4 5 0 9-3.2 9-7.2s-4-7.6-9-7.6z" fill="#09BB07"/><circle cx="8.7" cy="10.6" r="1.2" fill="#FFFFFF"/><circle cx="15.3" cy="10.6" r="1.2" fill="#FFFFFF"/></svg></button>
  <div class="rightDivider"></div>
  <div id="attachBar"></div>
  <div id="pcInbox"></div>
  <textarea id="inputBox" placeholder="在这里输入文字，可用输入法语音转文字" autocomplete="off"></textarea>
  <div class="sendRow">
    <button id="pcSendBtn">发送到电脑端<span class="btnSub">仅文本</span></button>
    <button id="sendBtn">粘到电脑文本框<span class="btnSub">文本和图片</span></button>
    <button id="attachBtn" aria-label="添加附件">+</button>
    <input type="file" id="cameraInput" accept="image/*" capture="environment" hidden>
    <input type="file" id="imageInput" accept="image/*" multiple hidden>
    <input type="file" id="docInput" accept="application/*,text/*" multiple hidden>
  </div>
  <div class="sideToggle"><span>粘到电脑后回车</span><input type="checkbox" id="enterToggle"></div>
  <button id="menuBtn" aria-label="菜单"><span class="bar"></span><span class="bar"></span><span class="bar"></span></button>
  <div id="status"></div>
  <div id="panel">
    <div class="panelHead">
      <span class="panelTitle">功能菜单</span>
      <button id="panelClose">×</button>
    </div>
    <div class="panelItem">电脑：<span id="pcName">未连接</span></div>
    <button id="inboxOpenBtn" class="panelNav"><span>查看收件箱</span><span>›</span></button>

    <button id="fullscreenBtn" class="panelNav"><span>全屏模式</span><span>›</span></button>
    <label class="panelToggle"><span>界面音效</span><input type="checkbox" id="uiSoundToggle"></label>
    <label class="panelToggle"><span>界面震动</span><input type="checkbox" id="uiVibeToggle"></label>
    <div class="panelNote">更多功能开发中…</div>
  </div>
</div>
<div id="attachSheetMask"></div>
<div id="attachSheet">
  <div class="sheetTitle">添加附件</div>
  <button id="cameraItem" class="sheetItem">拍照</button>
  <button id="galleryItem" class="sheetItem">从相册选择</button>
  <button id="fileItem" class="sheetItem">选择文件</button>
  <button id="sheetCancel" class="sheetCancel">取消</button>
</div>
<div id="kbMask"></div>
<div id="kbPanel">
  <div id="kbWrap">
    <div id="kbFuncRow">
      <div id="kbFuncPages">
        <div class="kbFuncPage">
          <div class="kbKey" data-act="settings" aria-label="键盘设置">⚙</div>
          <div class="kbKey kbMod" data-mod="win">Win</div>
          <div class="kbKey kbMod" data-mod="ctrl">Ctrl</div>
          <div class="kbKey kbMod" data-mod="shift">Shift</div>
          <div class="kbKey kbMod" data-mod="alt">Alt</div>
          <div class="kbKey small" data-combo="ctrl+c">复制</div>
          <div class="kbKey small" data-combo="ctrl+v">粘贴</div>
          <div class="kbKey" data-key="volumedown">🔉</div>
          <div class="kbKey" data-key="volumeup">🔊</div>
        </div>
        <div class="kbFuncPage">
          <div class="kbKey small" data-key="volumemute">静音</div>
          <div class="kbKey small" data-key="playpause">播放</div>
          <div class="kbKey small" data-key="prevtrack">上一曲</div>
          <div class="kbKey small" data-key="nexttrack">下一曲</div>
          <div class="kbKey small" data-combo="ctrl+shift+esc">任务管理器</div>
          <div class="kbKey small" data-combo="winleft+d">显示桌面</div>
        </div>
      </div>
    </div>
    <div id="kbPages">
      <div class="kbPage">
        <div class="kbRow">
          <div class="kbKey" data-key=";" data-shift=":">;</div>
          <div class="kbKey" data-key="'" data-shift="&quot;">'</div>
          <div class="kbKey" data-key="," data-shift="&lt;">,</div>
          <div class="kbKey" data-key="." data-shift="&gt;">.</div>
          <div class="kbKey" data-key="/" data-shift="?">/</div>
          <div class="kbKey" data-key="?">?</div>
          <div class="kbKey" data-key="-" data-shift="_">-</div>
          <div class="kbKey" data-key="_">_</div>
          <div class="kbKey" data-key="(">(</div>
          <div class="kbKey" data-key=")">)</div>
        </div>
        <div class="kbRow">
          <div class="kbKey" data-key="1">1</div>
          <div class="kbKey" data-key="2">2</div>
          <div class="kbKey" data-key="3">3</div>
          <div class="kbKey" data-key="4">4</div>
          <div class="kbKey" data-key="5">5</div>
          <div class="kbKey" data-key="6">6</div>
          <div class="kbKey" data-key="7">7</div>
          <div class="kbKey" data-key="8">8</div>
          <div class="kbKey" data-key="9">9</div>
          <div class="kbKey" data-key="0">0</div>
        </div>
        <div class="kbRow">
          <div class="kbKey" data-key="q" data-shift="Q">q</div>
          <div class="kbKey" data-key="w" data-shift="W">w</div>
          <div class="kbKey" data-key="e" data-shift="E">e</div>
          <div class="kbKey" data-key="r" data-shift="R">r</div>
          <div class="kbKey" data-key="t" data-shift="T">t</div>
          <div class="kbKey" data-key="y" data-shift="Y">y</div>
          <div class="kbKey" data-key="u" data-shift="U">u</div>
          <div class="kbKey" data-key="i" data-shift="I">i</div>
          <div class="kbKey" data-key="o" data-shift="O">o</div>
          <div class="kbKey" data-key="p" data-shift="P">p</div>
        </div>
        <div class="kbRow">
          <div class="kbKey" data-key="a" data-shift="A">a</div>
          <div class="kbKey" data-key="s" data-shift="S">s</div>
          <div class="kbKey" data-key="d" data-shift="D">d</div>
          <div class="kbKey" data-key="f" data-shift="F">f</div>
          <div class="kbKey" data-key="g" data-shift="G">g</div>
          <div class="kbKey" data-key="h" data-shift="H">h</div>
          <div class="kbKey" data-key="j" data-shift="J">j</div>
          <div class="kbKey" data-key="k" data-shift="K">k</div>
          <div class="kbKey" data-key="l" data-shift="L">l</div>
          <div class="kbKey small" data-key="backspace">退格</div>
        </div>
        <div class="kbRow">
          <div class="kbKey" data-key="z" data-shift="Z">z</div>
          <div class="kbKey" data-key="x" data-shift="X">x</div>
          <div class="kbKey" data-key="c" data-shift="C">c</div>
          <div class="kbKey" data-key="v" data-shift="V">v</div>
          <div class="kbKey" data-key="b" data-shift="B">b</div>
          <div class="kbKey" data-key="n" data-shift="N">n</div>
          <div class="kbKey" data-key="m" data-shift="M">m</div>
          <div class="kbKey wide" data-key="space">空格</div>
          <div class="kbKey small" data-key="enter">回车</div>
        </div>
      </div>
      <div class="kbPage">
        <div class="kbRow">
          <div class="kbKey small" data-key="esc">Esc</div>
          <div class="kbKey small" data-key="tab">Tab</div>
          <div class="kbKey small" data-key="capslock">Caps</div>

          <div class="kbKey small" data-key="delete">Del</div>
          <div class="kbKey small" data-key="insert">Ins</div>
          <div class="kbKey small" data-key="printscreen">PrtSc</div>
          <div class="kbKey small" data-key="backspace">退格</div>
        </div>
        <div class="kbRow">
          <div class="kbKey small" data-key="f1">F1</div>
          <div class="kbKey small" data-key="f2">F2</div>
          <div class="kbKey small" data-key="f3">F3</div>
          <div class="kbKey small" data-key="f4">F4</div>
          <div class="kbKey small" data-key="f5">F5</div>
          <div class="kbKey small" data-key="f6">F6</div>
          <div class="kbKey small" data-key="f7">F7</div>
          <div class="kbKey small" data-key="f8">F8</div>
          <div class="kbKey small" data-key="f9">F9</div>
          <div class="kbKey small" data-key="f10">F10</div>
        </div>
        <div class="kbRow">
          <div class="kbKey" data-key="left">←</div>
          <div class="kbKey" data-key="up">↑</div>
          <div class="kbKey" data-key="down">↓</div>
          <div class="kbKey" data-key="right">→</div>
          <div class="kbKey small" data-key="home">Home</div>
          <div class="kbKey small" data-key="end">End</div>
          <div class="kbKey small" data-key="pageup">PgUp</div>
          <div class="kbKey small" data-key="pagedown">PgDn</div>
        </div>
        <div class="kbRow">
          <div class="kbKey" data-key="!">!</div>
          <div class="kbKey" data-key="@">@</div>
          <div class="kbKey" data-key="#">#</div>
          <div class="kbKey" data-key="$">$</div>
          <div class="kbKey" data-key="%">%</div>
          <div class="kbKey" data-key="^">^</div>
          <div class="kbKey" data-key="&amp;">&amp;</div>
          <div class="kbKey" data-key="*">*</div>
          <div class="kbKey" data-key="[" data-shift="{">[</div>
          <div class="kbKey" data-key="]" data-shift="}">]</div>
        </div>
        <div class="kbRow">
          <div class="kbKey" data-key="=">=</div>
          <div class="kbKey" data-key="+">+</div>
          <div class="kbKey" data-key="\\">\\</div>
          <div class="kbKey" data-key="|">|</div>
          <div class="kbKey" data-key=":">:</div>
          <div class="kbKey" data-key="&quot;">"</div>
          <div class="kbKey" data-key="<">&lt;</div>
          <div class="kbKey" data-key=">">&gt;</div>
          <div class="kbKey small" data-key="f11">F11</div>
          <div class="kbKey small" data-key="f12">F12</div>
        </div>
      </div>
    </div>
    <div id="kbDots"><span class="on"></span><span></span></div>
  </div>
  <div id="kbSettings">
    <div class="kbSetHead"><button id="kbSetBack">‹ 返回</button><span>键盘设置</span></div>
    <label class="kbSetItem"><input type="checkbox" id="kbRepeatToggle"><span>长按按键连发</span></label>
    <label class="kbSetItem"><input type="checkbox" id="kbVibrateToggle"><span>按键震动反馈</span></label>
    <label class="kbSetItem"><input type="checkbox" id="kbSoundToggle"><span>按键音效</span></label>
    <label class="kbSetItem"><input type="checkbox" id="kbStickyToggle"><span>组合键后修饰键保持</span></label>
  </div>
</div>
<div id="mousePad">

  <div class="padMain">
    <div id="touchpad">
      <div id="touchHint">滑动=移动 · 轻点=左键 · 双击按住=拖拽 · 双指点按=右键</div>
    </div>
    <!-- kbBtn 必须是 #touchpad 的兄弟而非子元素：放内部时 touchend 冒泡进触控板手势
         处理器被 preventDefault，click 永不合成导致点击无效；兄弟定位视觉效果相同 -->
    <button id="kbBtn" aria-label="电脑键盘"><svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><rect x="2" y="5" width="20" height="14" rx="1.5" fill="none" stroke="#283246" stroke-width="1.6"/><path d="M5 9h1M8 9h1M11 9h1M14 9h1M17 9h1M5 12h1M8 12h1M11 12h1M14 12h1M17 12h1M7 15h10" stroke="#283246" stroke-width="1.4" fill="none" stroke-linecap="round"/></svg></button>
    <div class="btnRow">
      <button id="btnLeft" class="mouseBtn">左键</button>
      <button id="btnRight" class="mouseBtn">右键</button>
    </div>
  </div>
  <div id="scrollBar">
    <div id="scrollUp" class="scrollArrow">▲</div>
    <div id="scrollTrack"><div id="scrollThumb"></div></div>
    <div id="scrollDown" class="scrollArrow">▼</div>
  </div>
</div>
<script>
var statusEl = document.getElementById("status");
var menuBtn = document.getElementById("menuBtn");
var inputBox = document.getElementById("inputBox");
var sendBtn = document.getElementById("sendBtn");
var pcSendBtn = document.getElementById("pcSendBtn");
var panel = document.getElementById("panel");
var panelMask = document.getElementById("panelMask");
var pcName = document.getElementById("pcName");
var enterToggle = document.getElementById("enterToggle");

var attachBtn = document.getElementById("attachBtn");
var cameraInput = document.getElementById("cameraInput");
var imageInput = document.getElementById("imageInput");
var docInput = document.getElementById("docInput");
var attachSheet = document.getElementById("attachSheet");
var attachSheetMask = document.getElementById("attachSheetMask");
var attachBar = document.getElementById("attachBar");
var pendingFiles = [];
var inboxView = document.getElementById("inboxView");
var inboxList = document.getElementById("inboxList");
var inboxEmpty = document.getElementById("inboxEmpty");
var inboxErr = document.getElementById("inboxErr");

function loadEnterPref() {
  try {
    return localStorage.getItem("sendEnterEnabled") === "1";
  } catch (err) {
    return false;
  }
}

function saveEnterPref() {
  try {
    localStorage.setItem("sendEnterEnabled", enterToggle.checked ? "1" : "0");
  } catch (err) {}
}

enterToggle.checked = loadEnterPref();
enterToggle.addEventListener("change", saveEnterPref);


function renderFiles() {
  attachBar.innerHTML = "";
  if (!pendingFiles.length) {
    attachBar.style.display = "none";
    return;
  }
  attachBar.style.display = "flex";
  pendingFiles.forEach(function (file, index) {
    var chip = document.createElement("span");
    chip.className = "fileChip";
    var name = document.createElement("span");
    name.className = "fileName";
    name.textContent = file.name;
    var del = document.createElement("button");
    del.type = "button";
    del.className = "fileDel";
    del.textContent = "×";
    del.addEventListener("click", function () {
      pendingFiles.splice(index, 1);
      renderFiles();
    });
    chip.appendChild(name);
    chip.appendChild(del);
    attachBar.appendChild(chip);
  });
}

function addFiles(fileList) {
  for (var i = 0; i < fileList.length; i++) {
    pendingFiles.push(fileList[i]);
  }
  renderFiles();
}

function openAttachSheet() {
  attachSheet.classList.add("open");
  attachSheetMask.classList.add("open");
}

function closeAttachSheet() {
  attachSheet.classList.remove("open");
  attachSheetMask.classList.remove("open");
}

attachBtn.addEventListener("click", openAttachSheet);
attachSheetMask.addEventListener("click", function () { uiClick(); uiVibe(); closeAttachSheet(); });
document.getElementById("sheetCancel").addEventListener("click", closeAttachSheet);
document.getElementById("cameraItem").addEventListener("click", function () {
  closeAttachSheet();
  cameraInput.click();
});
document.getElementById("galleryItem").addEventListener("click", function () {
  closeAttachSheet();
  imageInput.click();
});
document.getElementById("fileItem").addEventListener("click", function () {
  closeAttachSheet();
  docInput.click();
});

cameraInput.addEventListener("change", function () {
  addFiles(cameraInput.files);
  cameraInput.value = "";
});

imageInput.addEventListener("change", function () {
  addFiles(imageInput.files);
  imageInput.value = "";
});

docInput.addEventListener("change", function () {
  addFiles(docInput.files);
  docInput.value = "";
});

var touchpad = document.getElementById("touchpad");
var touchHint = document.getElementById("touchHint");
var TP_HINT_DEFAULT = "滑动=移动 · 轻点=左键 · 双击按住=拖拽 · 双指点按=右键";
var hintFlashTimer = null;

function hintFlash(text) {
  touchHint.textContent = text;
  if (hintFlashTimer) {
    clearTimeout(hintFlashTimer);
  }
  hintFlashTimer = setTimeout(function () {
    touchHint.textContent = TP_HINT_DEFAULT;
    hintFlashTimer = null;
  }, 220);
}
var tpTouches = [];
var tpDx = 0;
var tpDy = 0;
var tpScrollAccum = 0;
var tpTimer = null;
var tpMaxTouch = 0;
var tpMoved = 0;
var tpStartTime = 0;
var tpFlushed = false;
var tpTapTimer = null;
var tpDragMode = false;
var TP_SCROLL_STEP = 12;
var TP_MOVE_SENSITIVITY = 3;
var SCROLL_CLICKS_PER_ARROW = 3;
var TP_TAP_DISTANCE = 12;
var TP_TAP_TIME = 350;
var TP_DRAG_WINDOW = 300;
var TP_TWO_TAP_DISTANCE = 8;

var mouseChain = Promise.resolve();
function sendMouse(body) {
  mouseChain = mouseChain.then(function () {
    return fetch("/mouse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (res) { return res.json(); }).then(function (data) {
      if (!data.ok) {
        updateStatus(data.message, "err");
      }
    }).catch(function () {});
  });
}

function tpFindTouch(id) {
  for (var i = 0; i < tpTouches.length; i++) {
    if (tpTouches[i].id === id) {
      return i;
    }
  }
  return -1;
}

function tpFlush() {
  var scroll = Math.round(tpScrollAccum / TP_SCROLL_STEP);
  if (scroll) {
    tpScrollAccum -= scroll * TP_SCROLL_STEP;
    tpFlushed = true;
    sendMouse({ dx: 0, dy: 0, scroll: scroll });
  }
  if (tpDx || tpDy) {
    tpFlushed = true;
    sendMouse({ dx: tpDx, dy: tpDy, scroll: 0 });
    tpDx = 0;
    tpDy = 0;
  }
}

function tpStopTimer() {
  if (tpTimer) {
    clearInterval(tpTimer);
    tpTimer = null;
  }
}

function tpResetGesture() {
  tpTouches = [];
  tpDx = 0;
  tpDy = 0;
  tpScrollAccum = 0;
  tpMaxTouch = 0;
  tpMoved = 0;
  tpStartTime = 0;
  tpFlushed = false;
}

function tpReleasePendingButtons() {
  // 手势被打断（页面隐藏/系统取消）时把挂起的左键收掉，杜绝卡键
  if (tpTapTimer) {
    clearTimeout(tpTapTimer);
    tpTapTimer = null;
    sendMouse({ buttonUp: "left" });
  }
  if (tpDragMode) {
    tpDragMode = false;
    sendMouse({ buttonUp: "left" });
  }
}

touchpad.addEventListener("touchstart", function (e) {
  e.preventDefault();
  if (document.activeElement === inputBox) {
    inputBox.blur();
  }
  for (var i = 0; i < e.changedTouches.length; i++) {
    var touch = e.changedTouches[i];
    if (tpFindTouch(touch.identifier) >= 0) {
      continue;
    }
    if (tpTouches.length === 0 && tpTapTimer && !tpDragMode) {
      // 第二次按下落在轻点等待窗口内 → 进入双击按住拖拽（左键保持按下）
      clearTimeout(tpTapTimer);
      tpTapTimer = null;
      tpDragMode = true;
      hintFlash("拖拽中");
    }
    tpTouches.push({
      id: touch.identifier,
      x: touch.clientX,
      y: touch.clientY
    });
  }
  tpMaxTouch = Math.max(tpMaxTouch, tpTouches.length);
  if (!tpStartTime) {
    tpStartTime = Date.now();
  }
  if (!tpTimer) {
    tpTimer = setInterval(tpFlush, 40);
  }
}, { passive: false });

touchpad.addEventListener("touchmove", function (e) {
  e.preventDefault();
  var sumDx = 0;
  var sumDy = 0;
  var count = 0;
  for (var i = 0; i < e.changedTouches.length; i++) {
    var touch = e.changedTouches[i];
    var index = tpFindTouch(touch.identifier);
    if (index < 0) {
      continue;
    }
    var item = tpTouches[index];
    sumDx += touch.clientX - item.x;
    sumDy += touch.clientY - item.y;
    item.x = touch.clientX;
    item.y = touch.clientY;
    count += 1;
  }
  if (!count) {
    return;
  }
  tpMoved += Math.abs(sumDx) + Math.abs(sumDy);
  if (tpDragMode || tpTouches.length < 2) {
    // 拖拽模式锁定单指语义（移动=按住左键拖），避免拖拽中触点变化误入滚动
    tpDx += sumDx * TP_MOVE_SENSITIVITY;
    tpDy += sumDy * TP_MOVE_SENSITIVITY;
  } else {
    tpScrollAccum += sumDy / count;
  }
}, { passive: false });

// 抬手判定：拖拽收尾 / 轻点左键 / 双指点按右键，与移动互斥（发过移动绝不判点）
touchpad.addEventListener("touchend", function (e) {
  e.preventDefault();
  for (var i = 0; i < e.changedTouches.length; i++) {
    var index = tpFindTouch(e.changedTouches[i].identifier);
    if (index >= 0) {
      tpTouches.splice(index, 1);
    }
  }
  if (tpTouches.length) {
    // 双指抬剩一根：双指点按右键在第一根手指抬起时判定
    if (tpMaxTouch === 2 && !tpFlushed && tpMoved < TP_TWO_TAP_DISTANCE &&
        Date.now() - tpStartTime < 400) {
      sendMouse({ rightClick: true });
      hintFlash("右键");
      tpMoved = TP_TAP_DISTANCE;
    }
    return;
  }
  if (tpDragMode) {
    sendMouse({ buttonUp: "left" });
    tpDragMode = false;
    hintFlash("拖拽完成");
  } else if (!tpFlushed && tpMaxTouch === 1 && tpMoved < TP_TAP_DISTANCE &&
             Date.now() - tpStartTime < TP_TAP_TIME) {
    // 单指轻点=左键：按下后留 300ms 窗口，期间第二次按下进入拖拽
    sendMouse({ buttonDown: "left" });
    hintFlash("左键");
    tpTapTimer = setTimeout(function () {
      sendMouse({ buttonUp: "left" });
      tpTapTimer = null;
    }, TP_DRAG_WINDOW);
  }
  tpFlush();
  tpStopTimer();
  tpResetGesture();
}, { passive: false });

touchpad.addEventListener("touchcancel", function (e) {
  e.preventDefault();
  tpFlush();
  tpStopTimer();
  tpReleasePendingButtons();
  tpResetGesture();
}, { passive: false });

document.addEventListener("visibilitychange", function () {
  if (document.hidden) {
    tpStopTimer();
    tpReleasePendingButtons();
    tpResetGesture();
  }
});

var scrollTrack = document.getElementById("scrollTrack");
var btnLeft = document.getElementById("btnLeft");
var btnRight = document.getElementById("btnRight");
var sbAccum = 0;
var sbLastY = 0;
var sbActive = false;
var SB_SCROLL_STEP = 5;

scrollTrack.addEventListener("touchstart", function (e) {
  e.preventDefault();
  sbActive = true;
  sbLastY = e.touches[0].clientY;
}, { passive: false });

scrollTrack.addEventListener("touchmove", function (e) {
  e.preventDefault();
  if (!sbActive) return;
  var y = e.touches[0].clientY;
  // 拖动对齐真鼠标滚轮方向：手指下滑=内容下移（原符号取反了）
  sbAccum += sbLastY - y;
  sbLastY = y;
  var scroll = Math.round(sbAccum / SB_SCROLL_STEP);
  if (scroll) {
    sbAccum -= scroll * SB_SCROLL_STEP;
    sendMouse({ dx: 0, dy: 0, scroll: scroll });
  }
}, { passive: false });

scrollTrack.addEventListener("touchend", function () {
  sbActive = false;
  var scroll = Math.round(sbAccum / SB_SCROLL_STEP);
  if (scroll) {
    sbAccum -= scroll * SB_SCROLL_STEP;
    sendMouse({ dx: 0, dy: 0, scroll: scroll });
  }
}, { passive: false });

scrollTrack.addEventListener("touchcancel", function () {
  sbActive = false;
}, { passive: false });

var scrollUp = document.getElementById("scrollUp");
var scrollDown = document.getElementById("scrollDown");
var arrowTimer = null;

function arrowScroll(dir) {
  sendMouse({ dx: 0, dy: 0, scroll: dir * SCROLL_CLICKS_PER_ARROW });
}

function startArrowRepeat(dir) {
  arrowScroll(dir);
  arrowTimer = setTimeout(function repeat() {
    arrowScroll(dir);
    arrowTimer = setTimeout(repeat, 100);
  }, 300);
}

function stopArrowRepeat() {
  if (arrowTimer) {
    clearTimeout(arrowTimer);
    arrowTimer = null;
  }
}

// ▲=滚轮向前推=内容上移（对齐真鼠标方向）；▲曾传 -1 导致内容下移，与鼠标相反
scrollUp.addEventListener("touchstart", function (e) {
  e.preventDefault();
  uiClick();
  uiVibe();
  startArrowRepeat(1);
}, { passive: false });
scrollDown.addEventListener("touchstart", function (e) {
  e.preventDefault();
  uiClick();
  uiVibe();
  startArrowRepeat(-1);
}, { passive: false });
scrollUp.addEventListener("touchend", stopArrowRepeat);
scrollUp.addEventListener("touchcancel", stopArrowRepeat);
scrollDown.addEventListener("touchend", stopArrowRepeat);
scrollDown.addEventListener("touchcancel", stopArrowRepeat);

btnLeft.addEventListener("touchstart", function (e) {
  e.preventDefault();
  uiClick();
  uiVibe();
  sendMouse({ click: true });
  hintFlash("左键");
}, { passive: false });

btnRight.addEventListener("touchstart", function (e) {
  e.preventDefault();
  uiClick();
  uiVibe();
  sendMouse({ rightClick: true });
  hintFlash("右键");
}, { passive: false });

var statusTimer = null;

function updateStatus(text, cls) {
  if (statusTimer) {
    clearTimeout(statusTimer);
    statusTimer = null;
  }
  statusEl.textContent = text;
  statusEl.className = cls || "";
  if (text && (cls === "err" || cls === "ok")) uiResult(cls === "ok");
  if (text && (cls === "err" || cls === "ok")) {
    statusTimer = setTimeout(function () {
      statusEl.textContent = "";
      statusEl.className = "";
      statusTimer = null;
    }, 4000);
  }
}

function setDot(cls) {
  menuBtn.className = cls || "";
}

var pcBell = document.getElementById("pcBell");
var pcInbox = document.getElementById("pcInbox");

function getPcLastRead() {
  try {
    return parseInt(localStorage.getItem("pcLastRead"), 10) || 0;
  } catch (err) {
    return 0;
  }
}

document.querySelector(".sideToggle").addEventListener("click", function () {
  uiClick();
  uiVibe();
});

var pcBellSeenId = null;

function updatePcBell(latestId) {
  // 新消息到达（计数增长）：叮一声 + 震动提示（页面首次加载静默，历史消息不响）
  if (pcBellSeenId !== null && latestId > pcBellSeenId) {
    uiDing();
    uiVibe();
  }
  pcBellSeenId = latestId;
  var hasRead = false;
  try {
    hasRead = localStorage.getItem("pcLastRead") !== null;
  } catch (err) {}
  if (!hasRead) {
    // 新设备/新浏览器首次使用：当前历史消息全部视为已读，不亮红点
    // 红点只提示本设备用过之后到达的新消息
    try {
      localStorage.setItem("pcLastRead", String(latestId));
    } catch (err) {}
    pcBell.classList.remove("hasUnread");
    return;
  }
  pcBell.classList.toggle("hasUnread", latestId > getPcLastRead());
}

function renderPcMsgs(msgs) {
  pcInbox.innerHTML = "";
  var latest = getPcLastRead();
  for (var i = msgs.length - 1; i >= 0; i--) {
    var item = msgs[i];
    var chip = document.createElement("div");
    chip.className = "pcChip";
    var timeDiv = document.createElement("div");
    timeDiv.className = "pcChipTime";
    timeDiv.textContent = (item.src === "phone" ? "我发送的" : "来自电脑") + " · " + item.time + " · 点击复制";
    var textDiv = document.createElement("div");
    textDiv.textContent = item.text;
    chip.appendChild(timeDiv);
    chip.appendChild(textDiv);
    chip.addEventListener("click", (function (msgText, timeEl) {
      return function () {
        var tmp = document.createElement("textarea");
        tmp.value = msgText;
        tmp.style.position = "fixed";
        tmp.style.opacity = "0";
        document.body.appendChild(tmp);
        tmp.select();
        var copied = false;
        try {
          copied = document.execCommand("copy");
        } catch (err) {}
        document.body.removeChild(tmp);
        if (copied) {
          timeEl.textContent = "已复制到手机剪贴板 ✓";
          chip.style.backgroundColor = "rgb(220, 240, 220)";
        } else {
          timeEl.textContent = "复制失败，请长按文字手动复制";
          chip.style.backgroundColor = "rgb(250, 220, 220)";
        }
      };
    })(item.text, timeDiv));
    pcInbox.appendChild(chip);
    if (item.id > latest) {
      latest = item.id;
    }
  }
  if (latest > getPcLastRead()) {
    try {
      localStorage.setItem("pcLastRead", String(latest));
    } catch (err) {}
  }
  updatePcBell(latest);
}

function togglePcInbox() {
  var willOpen = !pcInbox.classList.contains("open");
  pcInbox.classList.toggle("open", willOpen);
  inputBox.style.textIndent = willOpen ? "0" : "26px";
  inputBox.style.paddingTop = willOpen ? "0" : "9px";
  if (!willOpen) {
    return;
  }
  fetch("/pcmsgs?after=0").then(function (res) {
    return res.json();
  }).then(function (data) {
    if (data.ok && data.msgs && data.msgs.length) {
      renderPcMsgs(data.msgs);
    } else {
      pcInbox.innerHTML = '<div class="pcChip" style="color:rgb(140,150,175)">电脑还没有发来消息</div>';
    }
  }).catch(function () {
    pcInbox.innerHTML = '<div class="pcChip" style="color:rgb(220,60,60)">加载失败，请检查连接</div>';
  });
}

pcBell.addEventListener("click", togglePcInbox);

// 消息气泡"点外面即收起"：除铃铛（toggle 专用）/气泡本体（看消息与复制）/鼠标控制区
// （滑动不打扰气泡显示）外，任意触摸即收起，恢复输入框首行缩进——同键盘面板的场外收起交互
document.addEventListener("touchstart", function (e) {
  if (!pcInbox.classList.contains("open")) return;
  var el = e.target;
  while (el) {
    if (el === pcInbox || el === pcBell || el.id === "mousePad") return;
    el = el.parentElement;
  }
  togglePcInbox();
  uiClick();
  uiVibe();
}, { passive: true });

function checkConnection() {
  var ctrl = new AbortController();
  var timer = setTimeout(function () { ctrl.abort(); }, 3000);
  fetch("/ping", { signal: ctrl.signal }).then(function (res) { return res.json(); }).then(function (data) {
    clearTimeout(timer);
    setDot("ok");
    pcName.textContent = data.hostname || "未连接";
    updatePcBell(data.pcLatest || 0);
  }).catch(function () {
    clearTimeout(timer);
    setDot("err");
    pcName.textContent = "未连接";
  });
}

function sendText() {
  var text = inputBox.value;
  var hasFiles = pendingFiles.length > 0;
  if (!text.trim() && !hasFiles) {
    updateStatus("请先输入文字或添加附件", "err");
    return;
  }
  sendBtn.disabled = true;
  attachBtn.disabled = true;
  updateStatus("发送中…");
  var form = new FormData();
  form.append("text", text);
  form.append("pressEnter", enterToggle.checked ? "1" : "0");
  form.append("pasteImage", "1");
  pendingFiles.forEach(function (file) {
    form.append("files", file, file.name);
  });
  fetch("/send", { method: "POST", body: form })
    .then(function (res) { return res.json(); }).then(function (data) {
    sendBtn.disabled = false;
    attachBtn.disabled = false;
    if (data.ok) {
      inputBox.value = "";
      pendingFiles = [];
      renderFiles();
      if (data.imagePasted > 0) {
        updateStatus(data.message, "ok");
      } else {
        updateStatus("", "");
        uiResult(true);
      }
      inputBox.focus();
    } else {
      if (data.saved && data.saved.length) {
        pendingFiles = pendingFiles.filter(function (file) {
          return data.saved.indexOf(file.name) === -1;
        });
        renderFiles();
      }
      updateStatus(data.message, "err");
    }
  }).catch(function () {
    sendBtn.disabled = false;
    attachBtn.disabled = false;
    setDot("err");
    updateStatus("发送失败，请检查连接", "err");
  });
}

function openPanel() {
  panel.classList.add("open");
  panelMask.classList.add("open");
}

function closePanel() {
  panel.classList.remove("open");
  panelMask.classList.remove("open");
}

function formatSize(bytes) {
  if (bytes < 1024) return bytes + "B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + "KB";
  return (bytes / 1048576).toFixed(1) + "MB";
}

function formatTime(ts) {
  var d = new Date(ts * 1000);
  function pad(n) { return n < 10 ? "0" + n : "" + n; }
  return pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
}

function openInbox() {
  closePanel();
  inboxView.hidden = false;
  loadInbox();
}

function closeInbox() {
  inboxView.hidden = true;
}

function loadInbox() {
  inboxList.innerHTML = "";
  inboxErr.hidden = true;
  inboxEmpty.hidden = true;
  fetch("/inbox").then(function (res) { return res.json(); }).then(function (data) {
    if (!data.files.length) {
      inboxEmpty.hidden = false;
      return;
    }
    data.files.forEach(function (file) {
      var row = document.createElement("div");
      row.className = "inboxRow";
      if (file.isImage) {
        var thumb = document.createElement("img");
        thumb.className = "inboxThumb";
        thumb.src = "/inbox/preview?name=" + encodeURIComponent(file.name);
        thumb.alt = file.name;
        thumb.addEventListener("click", function () {
          openImagePreview(file.name);
        });
        row.appendChild(thumb);
      }
      var info = document.createElement("div");
      info.className = "inboxName";
      var name = document.createElement("div");
      name.className = "inboxFileName";
      name.textContent = file.name;
      var meta = document.createElement("div");
      meta.className = "inboxMeta";
      meta.textContent = formatSize(file.size) + " · " + formatTime(file.mtime);
      info.appendChild(name);
      info.appendChild(meta);
      var link = document.createElement("a");
      link.className = "fileDl";
      link.href = "/inbox/download?name=" + encodeURIComponent(file.name);
      link.setAttribute("download", file.name);
      link.textContent = "下载";
      row.appendChild(info);
      row.appendChild(link);
      inboxList.appendChild(row);
    });
  }).catch(function () {
    inboxErr.hidden = false;
  });
}

var imgViewer = document.getElementById("imgViewer");
var imgViewerImg = document.getElementById("imgViewerImg");

function openImagePreview(name) {
  imgViewerImg.src = "/inbox/preview?name=" + encodeURIComponent(name);
  imgViewer.hidden = false;
}

function closeImagePreview() {
  imgViewer.hidden = true;
  imgViewerImg.src = "";
}

document.getElementById("imgViewerClose").addEventListener("click", closeImagePreview);
imgViewer.addEventListener("click", function (e) {
  if (e.target === imgViewer) closeImagePreview();
});

menuBtn.addEventListener("click", openPanel);
document.getElementById("panelClose").addEventListener("click", closePanel);
panelMask.addEventListener("click", function () { uiClick(); uiVibe(); closePanel(); });
document.getElementById("inboxOpenBtn").addEventListener("click", openInbox);
document.getElementById("inboxClose").addEventListener("click", closeInbox);

// ---------- 全屏模式：隐藏浏览器地址栏/工具栏，触控板可用面积最大化 ----------
var fullscreenBtn = document.getElementById("fullscreenBtn");

function toggleFullscreen() {
  var root = document.documentElement;
  var active = document.fullscreenElement || document.webkitFullscreenElement;
  try {
    if (!active) {
      var req = root.requestFullscreen || root.webkitRequestFullscreen;
      var result = req ? req.call(root) : null;
      if (result && result.catch) {
        result.catch(function () {
          updateStatus("此浏览器不支持全屏", "err");
        });
      } else if (!req) {
        updateStatus("此浏览器不支持全屏", "err");
      }
    } else {
      var exit = document.exitFullscreen || document.webkitExitFullscreen;
      if (exit) exit.call(document);
    }
  } catch (err) {
    updateStatus("全屏失败：" + err.message, "err");
  }
  closePanel();
}

fullscreenBtn.addEventListener("click", toggleFullscreen);
document.addEventListener("fullscreenchange", function () {
  fullscreenBtn.firstElementChild.textContent =
    document.fullscreenElement ? "退出全屏" : "全屏模式";
});
document.addEventListener("webkitfullscreenchange", function () {
  fullscreenBtn.firstElementChild.textContent =
    (document.fullscreenElement || document.webkitFullscreenElement) ? "退出全屏" : "全屏模式";
});


function sendToDesktop() {
  var text = inputBox.value;
  if (!text.trim()) {
    updateStatus("请先输入文字", "err");
    return;
  }
  pcSendBtn.disabled = true;
  updateStatus("发送中…");
  fetch("/pcsend", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: text })
  }).then(function (res) { return res.json(); }).then(function (data) {
    pcSendBtn.disabled = false;
    if (data.ok) {
      inputBox.value = "";
      // 自己发的消息直接标为已读，避免触发自己的红点
      try {
        var lastRead = parseInt(localStorage.getItem("pcLastRead"), 10) || 0;
        if (data.id && data.id > lastRead) {
          localStorage.setItem("pcLastRead", String(data.id));
        }
      } catch (err) {}
      uiResult(true);
      updateStatus("", "");
    } else {
      updateStatus(data.message, "err");
    }
  }).catch(function () {
    pcSendBtn.disabled = false;
    setDot("err");
    updateStatus("发送失败，请检查连接", "err");
  });
}

sendBtn.addEventListener("click", sendText);
pcSendBtn.addEventListener("click", sendToDesktop);
checkConnection();
setInterval(checkConnection, 3000);

// ---------- 电脑键盘（触控板左上角按钮弹出，两页键位横向滑动翻页） ----------
var kbBtn = document.getElementById("kbBtn");
var kbMask = document.getElementById("kbMask");
var kbPanel = document.getElementById("kbPanel");
var kbPages = document.getElementById("kbPages");
var kbDots = document.getElementById("kbDots").children;
var kbFuncPages = document.getElementById("kbFuncPages");
var kbMods = { win: false, ctrl: false, shift: false, alt: false };
var kbModTimer = null;
var KB_MOD_KEYS = { win: "winleft", ctrl: "ctrl", shift: "shift", alt: "alt" };
var kbPage = 0;
var kbFuncPage = 0;
var kbFuncPageTotal = document.querySelectorAll("#kbFuncPages .kbFuncPage").length;
var kbChain = Promise.resolve();
var KB_REPEAT_KEYS = { backspace: 1, space: 1, delete: 1, up: 1, down: 1, left: 1, right: 1 };
var kbTouch = null;
var kbRepeatDelay = null;
var kbRepeatTimer = null;
var kbLongFired = false;

function kbRepeatOn() {
  try { return localStorage.getItem("kbRepeat") !== "0"; } catch (err) { return true; }
}
function kbVibrateOn() {
  try { return localStorage.getItem("kbVibrate") !== "0"; } catch (err) { return true; }
}
function kbStickyOn() {
  try { return localStorage.getItem("kbSticky") === "1"; } catch (err) { return false; }
}
function kbSoundOn() {
  try { return localStorage.getItem("kbSound") !== "0"; } catch (err) { return true; }
}

var kbAudioCtx = null;

function kbAudioInit() {
  if (!kbAudioCtx) {
    try {
      var AC = window.AudioContext || window.webkitAudioContext;
      if (AC) kbAudioCtx = new AC();
    } catch (err) {}
  }
  if (kbAudioCtx && kbAudioCtx.state === "suspended") kbAudioCtx.resume();
}

function kbTick() {
  // Web Audio 合成按键音：AudioContext 首次手势激活后任意时刻可播，
  // 不受 navigator.vibrate 的手势栈限制（长按/连发场景震动会被浏览器静默拒）。
  // suspended 状态也尝试播放（resume 已在 init 触发，部分浏览器排队生效）
  if (!kbSoundOn()) return;
  kbAudioInit();
  if (!kbAudioCtx) return;
  try {
    var t = kbAudioCtx.currentTime;
    var osc = kbAudioCtx.createOscillator();
    var g = kbAudioCtx.createGain();
    osc.type = "square";
    osc.frequency.value = 1200;
    g.gain.setValueAtTime(0.4, t);
    g.gain.exponentialRampToValueAtTime(0.001, t + 0.035);
    osc.connect(g);
    g.connect(kbAudioCtx.destination);
    osc.start(t);
    osc.stop(t + 0.04);
  } catch (err) {}
}

function kbBuzz() {
  kbTick();
  if (kbVibrateOn() && navigator.vibrate) navigator.vibrate([50, 30, 50]);
}

function uiSoundOn() {
  try { return localStorage.getItem("uiSound") !== "0"; } catch (err) { return true; }
}
function uiVibeOn() {
  try { return localStorage.getItem("uiVibe") !== "0"; } catch (err) { return true; }
}
function uiTone(freq, dur, vol, when) {
  kbAudioInit();
  if (!kbAudioCtx) return;
  try {
    var t = kbAudioCtx.currentTime + (when || 0);
    var osc = kbAudioCtx.createOscillator();
    var g = kbAudioCtx.createGain();
    osc.type = "square";
    osc.frequency.value = freq;
    g.gain.setValueAtTime(vol, t);
    g.gain.exponentialRampToValueAtTime(0.001, t + dur);
    osc.connect(g);
    g.connect(kbAudioCtx.destination);
    osc.start(t);
    osc.stop(t + dur + 0.01);
  } catch (err) {}
}
function uiClick() {
  if (!uiSoundOn()) return;
  uiTone(1200, 0.04, 0.4, 0);
}
function uiVibe() {
  if (!uiVibeOn()) return;
  if (navigator.vibrate) navigator.vibrate([50, 30, 50]);
}
function uiResult(okFlag) {
  if (!uiSoundOn()) return;
  if (okFlag) {
    uiTone(1318, 0.08, 0.35, 0);
    uiTone(1760, 0.12, 0.35, 0.09);
  } else {
    uiTone(420, 0.2, 0.4, 0);
  }
}
function uiDing() {
  if (!uiSoundOn()) return;
  uiTone(1318, 0.09, 0.35, 0);
  uiTone(1760, 0.15, 0.3, 0.1);
}
// 全局按钮点击确认：声音 tick + 震动双脉冲（委托，主界面/面板/附件面板按钮全覆盖）
document.addEventListener("click", function (e) {
  var el = e.target;
  while (el && el !== document.body && el.tagName !== "BUTTON") el = el.parentElement;
  if (el && el.tagName === "BUTTON") {
    uiClick();
    uiVibe();
  }
});


function kbSendKeys(keys) {
  kbChain = kbChain.then(function () {
    return fetch("/key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keys: keys })
    }).then(function (res) { return res.json(); }).then(function (data) {
      if (!data.ok) updateStatus(data.message || "按键失败", "err");
    }).catch(function () {});
  });
}

function kbRenderMods() {
  var btns = kbPanel.querySelectorAll(".kbMod");
  for (var i = 0; i < btns.length; i++) {
    btns[i].classList.toggle("on", !!kbMods[btns[i].getAttribute("data-mod")]);
  }
  kbSyncShift();
}

function kbSyncShift() {
  // Shift 锁定时键帽切换为上档字符（字母大写/Shift+标点），取消后恢复
  var keys = kbPanel.querySelectorAll("[data-shift]");
  var on = !!kbMods.shift;
  for (var i = 0; i < keys.length; i++) {
    var el = keys[i];
    el.textContent = on ? el.getAttribute("data-shift") : el.getAttribute("data-key");
  }
}

function kbClearMods() {
  kbMods.win = false;
  kbMods.ctrl = false;
  kbMods.shift = false;
  kbMods.alt = false;
  kbRenderMods();
}

function kbTapKey(key) {
  var keys = [];
  if (kbMods.win) keys.push("winleft");
  if (kbMods.ctrl) keys.push("ctrl");
  if (kbMods.alt) keys.push("alt");
  // 数字键不参与上档：Shift 锁定时仍输出数字本身（符号统一在第 2 页直按）
  if (kbMods.shift && !/^[0-9]$/.test(key)) keys.push("shift");
  keys.push(key);
  kbSendKeys(keys);
  if (keys.length > 1 && !kbStickyOn()) kbClearMods();
}

function kbStopRepeat() {
  if (kbRepeatDelay) { clearTimeout(kbRepeatDelay); kbRepeatDelay = null; }
  if (kbRepeatTimer) { clearInterval(kbRepeatTimer); kbRepeatTimer = null; }
}

function kbGoPage(index) {
  kbPage = index;
  kbPages.style.transform = "translateX(-" + (index * 50) + "%)";
  for (var i = 0; i < kbDots.length; i++) {
    kbDots[i].classList.toggle("on", i === index);
  }
}

function kbGoFuncPage(index) {
  kbFuncPage = Math.max(0, Math.min(index, kbFuncPageTotal - 1));
  kbFuncPages.style.width = (kbFuncPageTotal * 100) + "%";
  for (var i = 0; i < kbFuncPages.children.length; i++) {
    kbFuncPages.children[i].style.width = (100 / kbFuncPageTotal) + "%";
  }
  kbFuncPages.style.transform = "translateX(-" + (kbFuncPage * (100 / kbFuncPageTotal)) + "%)";
}

kbBtn.addEventListener("touchstart", function (e) {
  // 只拦触控板手势冒泡，不 preventDefault——click 才能触发开合
  e.stopPropagation();
}, { passive: true });
kbBtn.addEventListener("contextmenu", function (e) { e.preventDefault(); });
kbBtn.addEventListener("click", function () {
  kbAudioInit();
  kbClearMods();
  kbPanel.classList.remove("setting");
  kbGoPage(0);
  kbGoFuncPage(0);
  kbMask.classList.add("open");
  kbPanel.classList.add("open");
});
kbMask.addEventListener("click", function () {
  kbStopRepeat();
  uiClick();
  uiVibe();
  kbMask.classList.remove("open");
  kbPanel.classList.remove("open");
});

function bindKbToggle(id, storageKey, defaultChecked) {
  var el = document.getElementById(id);
  try {
    var saved = localStorage.getItem(storageKey);
    el.checked = saved === null ? defaultChecked : saved === "1";
  } catch (err) {
    el.checked = defaultChecked;
  }
  el.addEventListener("change", function () {
    try { localStorage.setItem(storageKey, el.checked ? "1" : "0"); } catch (err) {}
  });
}
bindKbToggle("kbRepeatToggle", "kbRepeat", true);
bindKbToggle("kbVibrateToggle", "kbVibrate", true);
bindKbToggle("kbSoundToggle", "kbSound", true);
bindKbToggle("kbStickyToggle", "kbSticky", false);
bindKbToggle("uiSoundToggle", "uiSound", true);
bindKbToggle("uiVibeToggle", "uiVibe", true);

document.getElementById("kbSetBack").addEventListener("click", function () {
  kbPanel.classList.remove("setting");
});

// 键帽手势（事件委托）：轻点发键、横向滑动翻页、长按连发
// isFuncRow 区分功能排与主键盘：两套滑动各自独立翻页，互不影响
function kbOnTouchStart(e, isFuncRow) {
  e.preventDefault();
  var t = e.changedTouches[0];
  var target = e.target.closest ? e.target.closest(".kbKey") : null;
  kbLongFired = false;
  kbTouch = { x: t.clientX, y: t.clientY, target: target, isFuncRow: isFuncRow };
  if (target) {
    target.classList.add("press");
    var key = target.getAttribute("data-key");
    var mod = target.getAttribute("data-mod");
    if (key && (key.length === 1 || KB_REPEAT_KEYS[key]) && kbRepeatOn()) {
      kbStopRepeat();
      kbRepeatDelay = setTimeout(function () {
        kbRepeatDelay = null;
        kbLongFired = true;
        kbRepeatTimer = setInterval(function () { kbTapKey(key); }, 120);
      }, 400);
    } else if (mod) {
      // 修饰键：短点=发单独按键（Win 弹开始菜单/Shift 切中英），长按=锁定供组合
      kbStopModTimer();
      kbModTimer = setTimeout(function () {
        kbModTimer = null;
        kbLongFired = true;
        kbMods[mod] = true;
        kbRenderMods();
        kbBuzz();
      }, 400);
    }
  }
}

function kbStopModTimer() {
  if (kbModTimer) { clearTimeout(kbModTimer); kbModTimer = null; }
}

function kbOnTouchMove(e) {
  e.preventDefault();
  if (kbModTimer && kbTouch) {
    var t = e.changedTouches[0];
    if (Math.abs(t.clientX - kbTouch.x) > 12 || Math.abs(t.clientY - kbTouch.y) > 12) {
      kbStopModTimer();
    }
  }
}

function kbOnTouchEnd(e) {
  kbStopRepeat();
  if (!kbTouch) return;
  if (kbTouch.target) kbTouch.target.classList.remove("press");
  var t = e.changedTouches[0];
  var dx = t.clientX - kbTouch.x;
  var dy = t.clientY - kbTouch.y;
  var target = kbTouch.target;
  var isFuncRow = kbTouch.isFuncRow;
  kbTouch = null;
  if (Math.abs(dx) > 40 && Math.abs(dx) > Math.abs(dy)) {
    if (isFuncRow) {
      kbGoFuncPage(kbFuncPage + (dx < 0 ? 1 : -1));
    } else {
      kbGoPage(dx < 0 ? Math.min(kbPage + 1, 1) : Math.max(kbPage - 1, 0));
    }
    return;
  }
  if (!target || Math.abs(dx) >= 12 || Math.abs(dy) >= 12 || kbLongFired) return;
  var act = target.getAttribute("data-act");
  var mod = target.getAttribute("data-mod");
  var combo = target.getAttribute("data-combo");
  var key = target.getAttribute("data-key");
  if (act === "settings") {
    kbPanel.classList.add("setting");
  } else if (mod) {
    if (kbModTimer) {
      // 短点：长按定时器未触发，发单独按键（win/ctrl/shift/alt 按下+抬起），顺带取消残留锁定
      kbStopModTimer();
      kbSendKeys([KB_MOD_KEYS[mod]]);
      kbMods[mod] = false;
      kbRenderMods();
    }
    kbBuzz();
  } else if (combo) {
    kbSendKeys(combo.split("+"));
    kbBuzz();
  } else if (key) {
    kbTapKey(key);
    kbBuzz();
  }
}

function kbOnTouchCancel() {
  kbStopRepeat();
  kbStopModTimer();
  if (kbTouch) {
    if (kbTouch.target) kbTouch.target.classList.remove("press");
    kbTouch = null;
  }
}

kbPages.addEventListener("touchstart", function (e) { kbOnTouchStart(e, false); }, { passive: false });
kbPages.addEventListener("touchmove", kbOnTouchMove, { passive: false });
kbPages.addEventListener("touchend", kbOnTouchEnd, { passive: false });
kbPages.addEventListener("touchcancel", kbOnTouchCancel, { passive: false });
kbFuncPages.addEventListener("touchstart", function (e) { kbOnTouchStart(e, true); }, { passive: false });
kbFuncPages.addEventListener("touchmove", kbOnTouchMove, { passive: false });
kbFuncPages.addEventListener("touchend", kbOnTouchEnd, { passive: false });
kbFuncPages.addEventListener("touchcancel", kbOnTouchCancel, { passive: false });

for (var kbDotIdx = 0; kbDotIdx < kbDots.length; kbDotIdx++) {
  (function (idx) {
    kbDots[idx].addEventListener("click", function () { kbGoPage(idx); });
  })(kbDotIdx);
}
</script>
</body>
</html>
"""


def getLanIps():
    ips = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        ips.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip != "127.0.0.1" and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def saveToClipboard(text):
    pyperclip.copy(text)
    return True


def pasteAtCursor():
    time.sleep(CLIPBOARD_DELAY_SECONDS)
    pyautogui.hotkey("ctrl", "v")


def nativeMouseButton(button):
    label = "左键" if button == "left" else "右键"
    if sendMouseEvent is None:
        if button == "left":
            pyautogui.click()
        else:
            pyautogui.rightClick()
        print("鼠标：%s点击（pyautogui 回退）" % label, flush=True)
        return
    if button == "left":
        down, up = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
    else:
        down, up = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
    sendMouseEventPair(down, up)
    print("鼠标：%s已注入（按下、抬起分两步提交）" % label, flush=True)


def releaseAllButtons():
    if sendMouseEvent is None:
        return
    # 只有确实处于按下状态才补一次抬起。无条件注入 RIGHTUP 会被部分窗口
    # 当成一次独立的右键抬起，从而在程序启动/页面打开后莫名弹出右键菜单。
    if user32.GetAsyncKeyState(VK_LBUTTON) & KEYSTATE_PRESSED:
        sendMouseEvent(MOUSEEVENTF_LEFTUP, quiet=True)
    if user32.GetAsyncKeyState(VK_RBUTTON) & KEYSTATE_PRESSED:
        sendMouseEvent(MOUSEEVENTF_RIGHTUP, quiet=True)


def saveAttachment(file):
    rawName = os.path.basename(str(file.filename or "").replace("\\", "/")).strip()
    rawName = re.sub(r'[\\/:*?"<>|]', "_", rawName).strip()
    if not rawName:
        rawName = "file"
    os.makedirs(ATTACH_DIR, exist_ok=True)
    base, ext = os.path.splitext(rawName)
    target = os.path.join(ATTACH_DIR, rawName)
    index = 1
    while os.path.exists(target):
        target = os.path.join(ATTACH_DIR, "%s(%d)%s" % (base, index, ext))
        index += 1
    file.save(target)
    return rawName, os.path.basename(target)


def writeClipboardImage(imagePath):
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$img=[System.Drawing.Image]::FromFile('"
        + imagePath.replace("'", "''") + "');"
        "[System.Windows.Forms.Clipboard]::SetImage($img);"
        "$img.Dispose()"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-EncodedCommand", encoded],
        capture_output=True,
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError("PowerShell 写入剪贴板失败")


def pasteImagesToCursor(imagePaths):
    pastedCount = 0
    for path in imagePaths:
        writeClipboardImage(path)
        time.sleep(CLIPBOARD_DELAY_SECONDS)
        pyautogui.hotkey("ctrl", "v")
        pastedCount += 1
        time.sleep(IMAGE_PASTE_DELAY_SECONDS)
    return pastedCount


@app.errorhandler(413)
def tooLarge(err):
    return jsonify(ok=False, message="文件过大，一次最多发送 %d MB" % MAX_UPLOAD_MB), 413


@app.get("/")
def index():
    return pageHtml


# ---------- 电脑→手机传信（桌面窗口发送，手机输入框右上角红点收取） ----------
pcMsgLock = threading.Lock()
pcMsgList = []  # [{id, text, time}]
PC_MSG_MAX = 50
pcMsgLastId = 0  # 最后一条消息的 id，保证严格单调递增（同毫秒多条也不重复）
# 消息记录落盘文件（exe 同级目录，绿色便携；不进收件箱，避免混入收件箱文件列表）
PC_MSG_FILE = os.path.join(BASE_DIR, "消息记录.json")


def loadPcMsgs():
    # 程序启动时恢复上次的消息记录（电脑端聊天流回放 + 手机端 /pcmsgs 可拉全量历史）
    global pcMsgLastId
    try:
        with open(PC_MSG_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            valid = [m for m in data if isinstance(m, dict) and "id" in m and "text" in m]
            with pcMsgLock:
                pcMsgList.extend(valid[-PC_MSG_MAX:])
                if pcMsgList:
                    pcMsgLastId = max(m["id"] for m in pcMsgList)
    except FileNotFoundError:
        pass  # 首次运行还没有消息记录
    except Exception as exc:
        print("消息记录恢复失败：%r" % exc, flush=True)


def savePcMsgs():
    # 调用方需已持有 pcMsgLock（在锁内快照并写盘，文件小写盘快）
    try:
        snapshot = [dict(m) for m in pcMsgList]
        with open(PC_MSG_FILE, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, ensure_ascii=False, indent=1)
    except Exception as exc:
        print("消息记录落盘失败：%r" % exc, flush=True)


loadPcMsgs()


def sendPcMsg(text, src="pc"):
    global pcMsgLastId
    text = str(text or "").strip()
    if not text:
        return False, "内容为空"
    if len(text) > MAX_TEXT_LENGTH:
        return False, "文字过长，最多 %d 字" % MAX_TEXT_LENGTH
    with pcMsgLock:
        pcMsgLastId = max(int(time.time() * 1000), pcMsgLastId + 1)
        pcMsgList.append({
            "id": pcMsgLastId,
            "text": text,
            "time": time.strftime("%H:%M:%S"),
            "src": src,
        })
        del pcMsgList[:-PC_MSG_MAX]
        savePcMsgs()
    return True, ""


@app.get("/pcmsgs")
def pcmsgs():
    after = int(request.args.get("after") or 0)
    with pcMsgLock:
        msgs = [dict(m) for m in pcMsgList if m["id"] > after]
    return jsonify(ok=True, msgs=msgs)


@app.post("/pcsend")
def pcsend():
    # 手机端"发送到电脑端"按钮：消息进入 pcMsgList，桌面窗口轮询后以左对齐聊天流展示
    data = request.get_json(silent=True) or {}
    ok, err = sendPcMsg(data.get("text"), src="phone")
    if not ok:
        return jsonify(ok=False, message=err)
    with pcMsgLock:
        latest = pcMsgList[-1]["id"] if pcMsgList else 0
    return jsonify(ok=True, id=latest)


@app.get("/ping")
def ping():
    with pcMsgLock:
        latest = pcMsgList[-1]["id"] if pcMsgList else 0
    return jsonify(ok=True, hostname=socket.gethostname(), pcLatest=latest)


@app.post("/send")
def send():
    text = str(request.form.get("text") or "")
    pressEnter = str(request.form.get("pressEnter") or "") == "1"
    pasteImage = str(request.form.get("pasteImage") or "") == "1"
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not text.strip() and not files:
        return jsonify(ok=False, message="内容为空")
    if len(text) > MAX_TEXT_LENGTH:
        return jsonify(ok=False, message="文字过长，最多 %d 字" % MAX_TEXT_LENGTH)
    saved = []
    imagePaths = []
    for item in files:
        ext = os.path.splitext(str(item.filename or ""))[1].lower()
        try:
            original, stored = saveAttachment(item)
        except Exception as exc:
            name = os.path.basename(str(item.filename or "?"))
            return jsonify(ok=False, saved=saved, message="文件「%s」保存失败：%s" % (name, exc))
        saved.append(original)
        if pasteImage and ext in IMAGE_EXTENSIONS:
            imagePaths.append(os.path.join(ATTACH_DIR, stored))
    enterSent = False
    pasted = False
    imagePasted = 0
    hasText = bool(text.strip())
    with pasteLock:
        if imagePaths and pressEnter:
            try:
                imagePasted = pasteImagesToCursor(imagePaths)
            except Exception as exc:
                return jsonify(ok=False, saved=saved, message="图片粘贴失败（原图已存入电脑收件箱），文字已保留：%s" % exc)
            if hasText:
                time.sleep(0.3)
        if hasText:
            try:
                clipboardOk = saveToClipboard(text)
            except Exception as exc:
                return jsonify(ok=False, saved=saved, message="剪贴板写入失败，文字已保留：%s" % exc)
            if not clipboardOk:
                return jsonify(ok=False, saved=saved, message="剪贴板校验未通过，文字已保留，请重试")
            pasted = True
            try:
                pasteAtCursor()
            except Exception:
                pasted = False
            if pasted and pressEnter:
                try:
                    time.sleep(ENTER_DELAY_SECONDS)
                    pyautogui.press("enter")
                    enterSent = True
                except Exception:
                    enterSent = False
        elif imagePasted:
            try:
                time.sleep(ENTER_DELAY_SECONDS)
                pyautogui.press("enter")
                enterSent = True
            except Exception:
                enterSent = False
    if not hasText:
        if imagePasted:
            return jsonify(ok=True, pasted=False, enterSent=enterSent, saved=saved, imagePasted=imagePasted, message="图片已粘贴，并存放至收件箱")
        if imagePaths:
            return jsonify(ok=True, pasted=False, enterSent=False, saved=saved, imagePasted=0, message="图片已存入电脑收件箱（开关未勾选，未粘贴）")
        return jsonify(ok=True, pasted=False, enterSent=False, saved=saved, imagePasted=0, message="文件已存入电脑收件箱")
    if pasted:
        if imagePaths and not imagePasted:
            return jsonify(ok=True, pasted=True, enterSent=enterSent, saved=saved, imagePasted=0, message="文字已粘贴到电脑光标处，图片仅存入收件箱（开关未勾选，未粘贴）")
        return jsonify(ok=True, pasted=True, enterSent=enterSent, saved=saved, imagePasted=imagePasted, message="已发送并粘贴到电脑光标处，并存放至收件箱")
    return jsonify(ok=True, pasted=False, enterSent=False, saved=saved, imagePasted=imagePasted, message="文字已存入电脑剪贴板，可手动 Ctrl+V")


@app.get("/inbox")
def inboxList():
    items = []
    if os.path.isdir(ATTACH_DIR):
        for name in os.listdir(ATTACH_DIR):
            path = os.path.join(ATTACH_DIR, name)
            if os.path.isfile(path):
                stat = os.stat(path)
                items.append({
                    "name": name,
                    "size": stat.st_size,
                    "mtime": int(stat.st_mtime),
                    "isImage": os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS,
                })
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return jsonify(ok=True, files=items)


@app.get("/inbox/download")
def inboxDownload():
    name = os.path.basename(str(request.args.get("name") or "").replace("\\", "/"))
    if not name or name in (".", ".."):
        return jsonify(ok=False, message="文件名无效"), 400
    path = os.path.join(ATTACH_DIR, name)
    if not os.path.isfile(path):
        return jsonify(ok=False, message="文件不存在"), 404
    return send_file(path, as_attachment=True, download_name=name)


@app.get("/inbox/preview")
def inboxPreview():
    name = os.path.basename(str(request.args.get("name") or "").replace("\\", "/"))
    if not name or name in (".", ".."):
        return jsonify(ok=False, message="文件名无效"), 400
    path = os.path.join(ATTACH_DIR, name)
    if not os.path.isfile(path):
        return jsonify(ok=False, message="文件不存在"), 404
    if os.path.splitext(name)[1].lower() not in IMAGE_EXTENSIONS:
        return jsonify(ok=False, message="不是图片"), 400
    return send_file(path, as_attachment=False)


@app.post("/mouse")
def mouse():
    data = request.get_json(silent=True) or {}
    dx = int(data.get("dx") or 0)
    dy = int(data.get("dy") or 0)
    scroll = int(data.get("scroll") or 0)
    actions = []
    try:
        with mouseLock:
            if dx or dy:
                pyautogui.moveRel(dx, dy)
                actions.append("移动(%d,%d)" % (dx, dy))
            if scroll:
                sendScroll(scroll)
                actions.append("滚轮%+d" % scroll)
            buttonDown = data.get("buttonDown")
            buttonUp = data.get("buttonUp")
            if buttonDown in ("left", "right"):
                # 拖拽手势：只按下不抬起（移动请求配合拖动，buttonUp 收尾）
                flags = MOUSEEVENTF_LEFTDOWN if buttonDown == "left" else MOUSEEVENTF_RIGHTDOWN
                sendMouseEvent(flags)
                actions.append("左键按下" if buttonDown == "left" else "右键按下")
            if buttonUp in ("left", "right"):
                flags = MOUSEEVENTF_LEFTUP if buttonUp == "left" else MOUSEEVENTF_RIGHTUP
                sendMouseEvent(flags)
                actions.append("左键抬起" if buttonUp == "left" else "右键抬起")
            if data.get("click") or data.get("rightClick"):
                # 只在按键动作前清理一次按键状态；纯移动/滚动时不再注入 LEFTUP/RIGHTUP，
                # 避免每帧抬手事件被目标窗口当成额外点击或打断拖拽
                releaseAllButtons()
            if data.get("click"):
                pos = pyautogui.position()
                print("鼠标：左键 @(%d,%d) 屏幕%s" % (pos.x, pos.y, pyautogui.size()), flush=True)
                nativeMouseButton("left")
                actions.append("左键")
            if data.get("rightClick"):
                pos = pyautogui.position()
                print("鼠标：右键 @(%d,%d) 屏幕%s" % (pos.x, pos.y, pyautogui.size()), flush=True)
                nativeMouseButton("right")
                actions.append("右键")
        if actions and (data.get("click") or data.get("rightClick") or
                        data.get("buttonDown") or data.get("buttonUp")):
            print("鼠标：%s" % " + ".join(actions), flush=True)
    except Exception as exc:
        print("鼠标操作失败：%r" % exc, flush=True)
        return jsonify(ok=False, message="鼠标操作失败：%s" % exc)
    return jsonify(ok=True)


@app.post("/key")
def keyPress():
    # 手机端"电脑键盘"：单键 pyautogui.press、组合键 hotkey
    # （Ctrl/Shift/Alt 在手机端锁定，点主键时随请求一次性组合发送）
    data = request.get_json(silent=True) or {}
    keys = data.get("keys")
    if not isinstance(keys, list) or not keys or len(keys) > 4:
        return jsonify(ok=False, message="按键参数不合法")
    norm = []
    for k in keys:
        k = str(k or "").strip()
        if not k or len(k) > 20:
            return jsonify(ok=False, message="按键参数不合法")
        if k.lower() in pyautogui.KEYBOARD_KEYS:
            norm.append(k.lower())
        elif len(k) == 1:
            norm.append(k)  # 单字符标点：keyboardMapping 覆盖，press 内部自动处理上档
        else:
            return jsonify(ok=False, message="不支持的按键：%s" % k)
    # Windows 不认合成的 Ctrl+Shift+Esc（任务管理器热键只响应物理键盘），
    # 直接启动 taskmgr：提权进程走 CreateProcess，普通进程走 ShellExecute（可能弹一次 UAC 确认）
    if norm == ["ctrl", "shift", "esc"]:
        err = None
        try:
            subprocess.Popen(["taskmgr"])
        except OSError as exc:
            try:
                os.startfile("taskmgr")
            except Exception as exc2:
                err = "%s / %s" % (exc, exc2)
        if err:
            print("启动任务管理器失败：%s" % err, flush=True)
            return jsonify(ok=False, message="启动任务管理器失败：%s" % err)
        print("键盘：已启动任务管理器", flush=True)
        return jsonify(ok=True)
    # 合成 Win+D 系统同样不认（实测），改走 Shell COM 的 ToggleDesktop（Win+D 的底层实现，再调一次恢复）
    if norm == ["winleft", "d"]:
        # COM 直调毫秒级，进程内完成无 shell 子进程（powershell 兜底已废弃：
        # 360 对 powershell.exe 报 Trojan.Generic 拦截）
        try:
            if toggleDesktopViaCom():
                print("键盘：已切换显示桌面（COM）", flush=True)
                return jsonify(ok=True)
            return jsonify(ok=False, message="显示桌面失败，请重试")
        except Exception as exc:
            print("COM ToggleDesktop 异常 %r" % exc, flush=True)
            return jsonify(ok=False, message="显示桌面失败：%s" % exc)
    # Win 短点：ctypes SendInput 直发合成 Win 键（remote-touchpad 同款通路，无 shell 子进程）
    if norm == ["winleft"]:
        try:
            if openStartMenu():
                print("键盘：已打开开始菜单", flush=True)
                return jsonify(ok=True)
            return jsonify(ok=False, message="打开开始菜单失败")
        except Exception as exc:
            print("打开开始菜单失败：%r" % exc, flush=True)
            return jsonify(ok=False, message="打开开始菜单失败：%s" % exc)
    try:

        with kbLock:
            if len(norm) == 1:
                pyautogui.press(norm[0])
            else:
                pyautogui.hotkey(*norm, interval=0)
    except Exception as exc:
        print("键盘注入失败：%r" % exc, flush=True)
        return jsonify(ok=False, message="按键失败：%r" % exc)
    return jsonify(ok=True)


def runDesktopApp(ips, serverErrorBox):
    # 桌面窗口：电脑→手机传信。Flask 在后台线程继续服务手机端。
    import tkinter as tk

    BG = "#E8ECF4"
    PANEL_BLUE = "#C4DEFE"
    BORDER = "#7896CD"
    FOCUS = "#5082EB"
    TEXT = "#191F32"
    MUTED = "#5A6580"
    RED = "#DC3C3C"
    GREEN = "#2B8F4C"
    FONT = ("微软雅黑", 10)
    FONT_BOLD = ("微软雅黑", 11, "bold")

    root = tk.Tk()
    root.title("手机输入 · 电脑端")
    root.minsize(430, 240)
    root.configure(bg=BG)

    def openAttachDir(event=None):
        try:
            os.startfile(ATTACH_DIR)
        except Exception:
            pass

    infoText1 = "服务运行中，手机浏览器访问：\n" + "\n".join(
        "http://%s:%d" % (ip, PORT) for ip in ips)
    infoBox1 = tk.Label(root, text=infoText1, bg=BG, fg=TEXT, font=FONT,
                        justify="left", anchor="w")
    infoBox1.pack(fill="x", padx=12, pady=(10, 0))

    folderParent = os.path.dirname(ATTACH_DIR)
    folderName = os.path.basename(ATTACH_DIR)
    inboxRow = tk.Frame(root, bg=BG)
    inboxRow.pack(fill="x", padx=12)
    inboxLabel = tk.Label(inboxRow, text="收件箱：" + folderParent + "\\",
                          bg=BG, fg=TEXT, font=FONT)
    inboxLabel.pack(side="left")
    inboxLink = tk.Label(inboxRow, text=folderName, bg=BG, fg=FOCUS, font=FONT,
                         cursor="hand2")
    inboxLink.pack(side="left")
    inboxLink.bind("<Button-1>", openAttachDir)

    infoText2 = "首次运行若弹出防火墙提示，请勾选允许（专用网络）\n" + \
        "若电脑装有安全软件，建议将本程序加入白名单，否则可能影响触控板"
    infoBox2 = tk.Label(root, text=infoText2, bg=BG, fg=TEXT, font=FONT,
                        justify="left", anchor="w")
    infoBox2.pack(fill="x", padx=12)

    tipLabel = tk.Label(root, text="发送到手机的文字（链接等）：", bg=BG, fg=TEXT, font=FONT)
    tipLabel.pack(anchor="w", padx=12)

    # 输入卡片（聊天界面式）：上半展示区 + 灰色分隔线 + 下半输入区 + 发送按钮内嵌右下角
    shell = tk.Frame(root, bg="#FFFFFF", highlightthickness=1,
                     highlightbackground=BORDER, highlightcolor=FOCUS)
    # expand=True + fill="both"：拉伸窗口时槽位变高且卡片纵向填满槽位，
    # 卡片真实增高 → 内部消息展示框（chatList expand）跟着变高。
    # 若只写 fill="x"，卡片会垂直居中于变高的槽位，上下各留一半空白
    shell.pack(fill="both", expand=True, padx=12, pady=(4, 0))

    # PanedWindow：展示区与输入区之间的分隔条可自由拖动，调节上下比例
    paned = tk.PanedWindow(shell, orient="vertical", bd=0,
                           sashwidth=2, bg="#B9C6E1", sashrelief="flat")
    paned.pack(fill="both", expand=True, padx=8, pady=6)

    chatList = tk.Text(paned, font=FONT, height=6, bd=0, highlightthickness=0,
                       bg="#FFFFFF", fg=TEXT, wrap="word", padx=10, pady=6,
                       state="normal")
    # 每行所有字符（时间/内容/勾/换行）的 justify 必须显式一致——
    # Tk Text 在一行内多 tag justify 不一致时结果不确定（time=right 曾把
    # phoneMsg 左对齐的行拽回右边）。电脑行全 right、手机行全 left。
    chatList.tag_config("timeL", foreground="#8C96AF", font=("微软雅黑", 9),
                        justify="left")
    chatList.tag_config("timeR", foreground="#8C96AF", font=("微软雅黑", 9),
                        justify="right")
    chatList.tag_config("msg", foreground="#283246", justify="right")
    chatList.tag_config("phoneMsg", foreground="#283246", justify="left")
    chatList.tag_config("ok", foreground=GREEN, justify="right")
    chatList.tag_config("hint", foreground="#9AA3B8", justify="right")
    chatList.insert("1.0", "发送到手机的消息会显示在这里", "hint")
    chatList.config(state="disabled")
    paned.add(chatList, minsize=70, stretch="always")
    chatHint = [True]

    inputWrap = tk.Frame(paned, bg="#FFFFFF")
    msgText = tk.Text(inputWrap, font=FONT, height=3, bd=0, highlightthickness=0,
                      bg="#FFFFFF", fg="#283246", wrap="char")
    msgText.pack(fill="both", expand=True, pady=(6, 34))
    sendBtn = tk.Button(inputWrap, text="发送", font=FONT_BOLD, bg=PANEL_BLUE, fg=TEXT,
                        relief="flat", bd=0, activebackground="#B0D2FC",
                        activeforeground=TEXT, cursor="hand2", width=6,
                        command=lambda: doSend())
    # 必须有 rely=1.0：把按钮锚到输入区底部内侧，否则按钮会跑到输入区外
    sendBtn.place(in_=inputWrap, relx=1.0, rely=1.0, x=-8, y=-8, anchor="se")
    paned.add(inputWrap, minsize=100, stretch="never")

    statusLabel = tk.Label(root, text="", bg=BG, fg=TEXT, font=FONT, anchor="w")

    def showStatus(text, color):
        # 无提示时不占位：日志按钮才能紧贴输入卡片下边线
        if text:
            statusLabel.config(text=text, fg=color)
            statusLabel.pack(fill="x", padx=14, pady=(1, 0), after=shell)
        else:
            statusLabel.pack_forget()

    def appendChat(text, phone=False, timeStr=None):
        # 聊天流：时间灰字 + 消息内容一行，自动滚到底部；
        # 电脑发的全行右对齐（timeR+msg+ok），手机发来的全行左对齐（timeL+phoneMsg）。
        # 行内所有字符 justify 必须同值——Tk Text 多 tag justify 不一致时结果不确定
        bodyTag = "phoneMsg" if phone else "msg"
        timeTag = "timeL" if phone else "timeR"
        chatList.config(state="normal")
        if chatHint[0]:
            chatList.delete("1.0", "end")
            chatHint[0] = False
        chatList.insert("end", (timeStr or time.strftime("%H:%M:%S")) + "  ", timeTag)
        chatList.insert("end", text, bodyTag)
        if not phone:
            chatList.insert("end", " ✓", "ok")
        chatList.insert("end", "\n", bodyTag)
        chatList.see("end")
        chatList.config(state="disabled")

    # 启动时回放上次的消息记录到聊天流（持久化：下次打开还在）
    with pcMsgLock:
        history = [dict(m) for m in pcMsgList]
    for m in history:
        appendChat(m["text"], phone=(m.get("src") == "phone"), timeStr=m["time"])

    # 运行日志：默认折叠，点按钮展开向下撑开窗口，再点收起
    logOpen = [False]
    logWrap = tk.Frame(root, bg="#FFFFFF", highlightthickness=1,
                       highlightbackground=BORDER)
    logText = tk.Text(logWrap, font=FONT, height=6, bd=0, highlightthickness=0,
                      bg="#FFFFFF", fg=TEXT, wrap="word", padx=6, pady=4,
                      state="disabled")
    logText.pack(fill="both", expand=True, padx=(8, 18), pady=3)
    logScroll = tk.Scrollbar(logWrap, command=logText.yview, width=12)
    # 滚动条悬浮内嵌在日志容器右缘，不向外突出
    logScroll.place(relx=1.0, rely=0, relheight=1.0, anchor="ne")
    logText.config(yscrollcommand=logScroll.set)

    def appendLog(msg):
        logText.config(state="normal")
        logText.insert("end", "[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
        logText.see("end")
        logText.config(state="disabled")

    def syncWindowHeight():
        root.update_idletasks()
        root.geometry("460x%d" % max(root.winfo_reqheight(), 240))

    def toggleLog():
        logOpen[0] = not logOpen[0]
        if logOpen[0]:
            logBtn.config(text="▴ 运行日志")
            logWrap.pack(fill="x", padx=11, pady=(2, 0), after=logBtn)
            # 展开日志：只有当窗口高度小于内容需求时才撑开；
            # 用户已拉大的窗口让消息展示框自行收缩吸收，不回弹
            root.update_idletasks()
            if root.winfo_height() < root.winfo_reqheight():
                root.geometry("460x%d" % root.winfo_reqheight())
        else:
            logBtn.config(text="▾ 运行日志")
            logWrap.pack_forget()
            # 收起日志：不回弹窗口，释放的空间由消息展示框吸收

    logBtn = tk.Button(root, text="▾ 运行日志", font=FONT, bg=BG, fg=MUTED,
                       relief="flat", bd=0, cursor="hand2", anchor="w",
                       activebackground=BG, activeforeground=TEXT,
                       command=toggleLog)
    # 紧贴输入卡片下边线（中间的状态提示只在有内容时出现）
    logBtn.pack(fill="x", padx=14, pady=(2, 0), after=shell)

    def doSend(event=None):
        text = msgText.get("1.0", "end").strip()
        if not text:
            showStatus("请先输入内容", RED)
            return "break"
        ok, err = sendPcMsg(text)
        if ok:
            msgText.delete("1.0", "end")
            # 成功信号 = 消息行尾的绿勾（对齐主项目"剪贴板成功才清空"契约，不再单独显示"已发送"提示）
            showStatus("", GREEN)
            appendChat(text)
            appendLog("电脑 → 手机：%s" % text)
        else:
            showStatus(err, RED)
            appendLog("发送失败：%s" % err)
        return "break"

    msgText.bind("<Return>", doSend)

    def onShellFocusIn(event):
        shell.config(highlightbackground=FOCUS)

    def onShellFocusOut(event):
        shell.config(highlightbackground=BORDER)
    msgText.bind("<FocusIn>", onShellFocusIn)
    msgText.bind("<FocusOut>", onShellFocusOut)

    def checkServerError():
        if serverErrorBox:
            infoBox.config(fg=RED, text="服务启动失败：%s\n请关闭旧的服务窗口后重新打开本程序。" % serverErrorBox[0])
            appendLog("服务启动失败：%s" % serverErrorBox[0])
    root.after(1500, checkServerError)

    # 轮询手机发到桌面端的消息（pcMsgList 里 src="phone" 的），左对齐进聊天流；
    # 起点设为历史最后一条的 id——历史已在上面回放过，从 0 起算会把旧消息重复显示一遍
    guiLastShown = [history[-1]["id"] if history else 0]

    def pollPhoneMsgs():
        with pcMsgLock:
            news = [m for m in pcMsgList
                    if m.get("src") == "phone" and m["id"] > guiLastShown[0]]
            if pcMsgList:
                guiLastShown[0] = pcMsgList[-1]["id"]
        for m in news:
            appendChat(m["text"], phone=True, timeStr=m["time"])
            appendLog("手机 → 桌面：%s" % m["text"])
        root.after(2000, pollPhoneMsgs)
    root.after(2000, pollPhoneMsgs)

    root.update_idletasks()
    root.geometry("460x%d" % max(root.winfo_reqheight(), 240))
    appendLog("服务已启动，等待手机连接（手机打开上方地址即可）")
    root.mainloop()


def main():
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    try:
        releaseAllButtons()
    except Exception:
        pass
    ips = getLanIps()
    print("=" * 52)
    print("局域网文字投递工具 已启动（桌面窗口模式）")
    print("电脑名：%s" % socket.gethostname())
    print("手机浏览器访问以下地址（需与电脑同一 Wi-Fi）：")
    for ip in ips:
        print("  http://%s:%d" % (ip, PORT))
    print("收件箱：%s" % ATTACH_DIR)
    print("=" * 52)
    serverErrorBox = []

    def runServer():
        try:
            app.run(host=HOST, port=PORT, debug=False)
        except Exception as exc:
            serverErrorBox.append(str(exc))

    threading.Thread(target=runServer, daemon=True).start()
    runDesktopApp(ips, serverErrorBox)


if __name__ == "__main__":
    main()