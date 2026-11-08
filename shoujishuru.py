# -*- coding: utf-8 -*-
import base64
import ctypes
import logging
import os
import re
import socket
import subprocess
import threading
import time

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
    VK_LBUTTON = 0x01
    VK_RBUTTON = 0x02
    KEYSTATE_PRESSED = 0x8000

    def sendMouseEvent(flags, quiet=False):
        event = _INPUT()
        event.type = INPUT_MOUSE
        event.union.mi = _MOUSEINPUT(0, 0, 0, flags, 0, None)
        result = user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_INPUT))
        if result == 0 and not quiet:
            err = ctypes.get_last_error()
            print("鼠标：SendInput 被系统拦截（错误码 %d），尝试 mouse_event 兜底" % err, flush=True)
            user32.mouse_event(flags, 0, 0, 0, 0)

    def sendMouseEventPair(down, up):
        events = (_INPUT * 2)()
        events[0].type = INPUT_MOUSE
        events[0].union.mi = _MOUSEINPUT(0, 0, 0, down, 0, None)
        events[1].type = INPUT_MOUSE
        events[1].union.mi = _MOUSEINPUT(0, 0, 0, up, 0, None)
        result = user32.SendInput(2, events, ctypes.sizeof(_INPUT))
        if result == 2:
            return
        err = ctypes.get_last_error()
        vk = VK_LBUTTON if down == MOUSEEVENTF_LEFTDOWN else VK_RBUTTON
        user32.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.01)
        if not (user32.GetAsyncKeyState(vk) & KEYSTATE_PRESSED):
            raise RuntimeError(
                "鼠标注入通道被系统拦截（SendInput err=%d，mouse_event 兜底也无效）——"
                "请退出华为电脑管家/杀毒软件/远控软件后重试，或重启电脑" % err
            )
        user32.mouse_event(up, 0, 0, 0, 0)
        time.sleep(0.01)
        if user32.GetAsyncKeyState(vk) & KEYSTATE_PRESSED:
            user32.mouse_event(up, 0, 0, 0, 0)
            time.sleep(0.01)
            if user32.GetAsyncKeyState(vk) & KEYSTATE_PRESSED:
                raise RuntimeError("鼠标抬起被拦截，可能卡键——请物理点一下鼠标解除")
        print("鼠标：SendInput 被拦截（err=%d），mouse_event 兜底成功（回执确认）" % err, flush=True)
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
ATTACH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "收件箱")
MAX_UPLOAD_MB = 200

pasteLock = threading.Lock()
mouseLock = threading.Lock()

pyautogui.PAUSE = 0
pyautogui.FAILSAFE = False

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

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
.title {
  font-size: 20px;
  font-weight: 600;
  text-align: center;
  color: rgb(40, 70, 140);
  margin: 0 0 8px;
}
.inputShell {
  border: 1px solid rgb(185, 198, 225);
  border-radius: 12px;
  background: rgb(255, 255, 255);
  box-shadow: 0 2px 10px rgba(180, 195, 225, 0.35);
  height: 38vh;
  display: flex;
  flex-direction: column;
}
.inputShell:focus-within { border-color: rgb(80, 130, 235); }
textarea {
  width: 100%;
  flex: 1;
  min-height: 0;
  resize: none;
  background: transparent;
  border: none;
  border-radius: 12px;
  color: rgb(40, 50, 70);
  font-size: 17px;
  line-height: 1.5;
  padding: 12px 14px;
  outline: none;
  -webkit-appearance: none;
  appearance: none;
}
button {
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
  gap: 8px;
  margin-top: 8px;
}
#sendBtn {
  flex: 1;
  margin-top: 0;
  height: 46px;
  padding: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}
#attachBtn {
  width: 64px;
  margin-top: 0;
  height: 46px;
  padding: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;
  font-weight: 400;
  line-height: 1;
}
#attachBar {
  display: none;
  flex-wrap: wrap;
  gap: 6px;
  padding: 10px 10px 0 10px;
  max-height: 96px;
  overflow-y: auto;
  flex-shrink: 0;
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
  margin-top: 8px;
  height: 40vh;
  background: rgb(255, 255, 255);
  border: 1px solid rgb(185, 198, 225);
  border-radius: 12px;
  box-shadow: 0 2px 10px rgba(180, 195, 225, 0.35);
  display: flex;
  overflow: hidden;
}
.padMain {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
#touchpad {
  flex: 1;
  min-height: 0;
  touch-action: none;
  user-select: none;
  -webkit-user-select: none;
  display: flex;
  align-items: center;
  justify-content: center;
}
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
#scrollUp { border-bottom: 1px solid rgb(185, 198, 225); }
#scrollDown { border-top: 1px solid rgb(185, 198, 225); }
.scrollArrow:active { background: rgb(198, 221, 255); }
#scrollTrack {
  flex: 1;
  position: relative;
  touch-action: none;
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
#btnLeft { border-right: 1px solid rgb(185, 198, 225); }
#connDot {
  position: fixed;
  top: 14px;
  right: 14px;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: rgb(170, 178, 190);
}
#connDot.ok { background: rgb(52, 199, 89); }
#connDot.err { background: rgb(255, 69, 58); }
#status {
  margin-top: 8px;
  text-align: center;
  font-size: 13px;
  color: rgb(135, 148, 172);
}
#status.ok { color: rgb(35, 150, 95); }
#status.err { color: rgb(205, 75, 75); }
#status:empty { display: none; }
#menuBtn {
  position: fixed;
  top: 4px;
  left: 6px;
  width: 40px;
  height: 40px;
  margin-top: 0;
  padding: 0;
  border: none;
  border-radius: 10px;
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
  position: fixed;
  top: 0;
  left: 0;
  bottom: 0;
  width: 33.33vw;
  min-width: 132px;
  background: rgb(255, 255, 255);
  border-right: 1px solid rgb(185, 198, 225);
  box-shadow: 4px 0 16px rgba(120, 140, 180, 0.25);
  transform: translateX(-105%);
  transition: transform 0.25s ease;
  z-index: 50;
  padding: 14px 12px;
}
#panel.open { transform: translateX(0); }
.panelHead {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
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
  padding: 8px 4px;
  border-bottom: 1px solid rgb(232, 236, 244);
}
.panelItem span { color: rgb(40, 70, 140); font-weight: 500; }
.panelToggle {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 13px;
  color: rgb(70, 85, 115);
  padding: 8px 4px;
  border-bottom: 1px solid rgb(232, 236, 244);
}
.panelToggle input {
  width: 16px;
  height: 16px;
  margin: 0;
  accent-color: rgb(64, 110, 255);
}
.panelNote {
  margin-top: 10px;
  font-size: 12px;
  color: rgb(160, 170, 190);
}
.panelNav {
  width: 100%;
  margin-top: 0;
  padding: 8px 4px;
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
</style>
</head>
<body>
<button id="menuBtn" aria-label="菜单"><span class="bar"></span><span class="bar"></span><span class="bar"></span></button>
<div id="panelMask"></div>
<div id="panel">
  <div class="panelHead">
    <span class="panelTitle">功能菜单</span>
    <button id="panelClose">×</button>
  </div>
  <div class="panelItem">电脑：<span id="pcName">—</span></div>
  <label class="panelToggle"><span>粘贴后回车</span><input type="checkbox" id="enterToggle"></label>
  <label class="panelToggle"><span>图片贴进框</span><input type="checkbox" id="imageToggle"></label>

  <button id="inboxOpenBtn" class="panelNav"><span>查看收件箱</span><span>›</span></button>
  <div class="panelNote">更多功能开发中…</div>
</div>
<div id="inboxView" hidden>
  <div class="inboxHead">
    <span class="panelTitle">电脑收件箱</span>
    <button id="inboxClose">×</button>
  </div>
  <div id="inboxList"></div>
  <div id="inboxEmpty" hidden>收件箱还是空的</div>
  <div id="inboxErr" hidden>加载失败，请检查连接</div>
</div>
<div id="connDot"></div>
<div class="title">文字投递到电脑</div>
<div class="inputShell">
  <div id="attachBar"></div>
  <textarea id="inputBox" placeholder="在这里输入文字，可用输入法语音转文字" autocomplete="off"></textarea>
</div>
<div class="sendRow">
  <button id="sendBtn">发送到电脑</button>
  <button id="attachBtn" aria-label="添加附件">+</button>
  <input type="file" id="cameraInput" accept="image/*" capture="environment" hidden>
  <input type="file" id="imageInput" accept="image/*" multiple hidden>
  <input type="file" id="docInput" accept="application/*,text/*" multiple hidden>
</div>
<div id="attachSheetMask"></div>
<div id="attachSheet">
  <div class="sheetTitle">添加附件</div>
  <button id="cameraItem" class="sheetItem">拍照</button>
  <button id="galleryItem" class="sheetItem">从相册选择</button>
  <button id="fileItem" class="sheetItem">选择文件</button>
  <button id="sheetCancel" class="sheetCancel">取消</button>
</div>
<div id="status"></div>
<div id="mousePad">
  <div class="padMain">
    <div id="touchpad">
      <div id="touchHint">滑动=移动鼠标 · 轻点=左键 · 双指点按=右键</div>
    </div>
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
var connDot = document.getElementById("connDot");
var inputBox = document.getElementById("inputBox");
var sendBtn = document.getElementById("sendBtn");
var panel = document.getElementById("panel");
var panelMask = document.getElementById("panelMask");
var pcName = document.getElementById("pcName");
var enterToggle = document.getElementById("enterToggle");
var imageToggle = document.getElementById("imageToggle");
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

function loadImagePref() {
  try {
    return localStorage.getItem("sendImageEnabled") === "1";
  } catch (err) {
    return false;
  }
}

function saveImagePref() {
  try {
    localStorage.setItem("sendImageEnabled", imageToggle.checked ? "1" : "0");
  } catch (err) {}
}

imageToggle.checked = loadImagePref();
imageToggle.addEventListener("change", saveImagePref);

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
attachSheetMask.addEventListener("click", closeAttachSheet);
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
var TP_HINT_DEFAULT = "滑动=移动鼠标 · 轻点=左键 · 双指点按=右键";
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
var tpMode = null;
var tpLastX = 0;
var tpLastY = 0;
var tpMoved = 0;
var tpStart = 0;
var tpDx = 0;
var tpDy = 0;
var tpScrollAccum = 0;
var tpTimer = null;
var tpFlushed = false;
var tpMaxTouch = 0;
var TP_TAP_DISTANCE = 12;
var TP_TAP_TIME = 350;
var TP_TWO_TAP_DISTANCE = 8;
var TP_TWO_TAP_TIME = 400;
var TP_SCROLL_STEP = 18;
var TP_MOVE_SENSITIVITY = 3;
var SCROLL_CLICKS_PER_ARROW = 5;

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

function tpFlush() {
  var scroll = Math.round(tpScrollAccum / TP_SCROLL_STEP);
  if (scroll) {
    tpScrollAccum -= scroll * TP_SCROLL_STEP;
    sendMouse({ dx: 0, dy: 0, scroll: scroll });
    tpFlushed = true;
  }
  if (tpDx || tpDy) {
    sendMouse({ dx: tpDx, dy: tpDy, scroll: 0 });
    tpDx = 0;
    tpDy = 0;
    tpFlushed = true;
  }
}

function tpStopTimer() {
  if (tpTimer) {
    clearInterval(tpTimer);
    tpTimer = null;
  }
}

touchpad.addEventListener("touchstart", function (e) {
  e.preventDefault();
  if (!tpTimer) {
    tpTimer = setInterval(tpFlush, 40);
  }
  if (e.touches.length === 1) {
    tpFlushed = false;
    tpMaxTouch = 1;
  } else if (e.touches.length > tpMaxTouch) {
    tpMaxTouch = e.touches.length;
  }
  if (e.touches.length >= 2) {
    tpMode = "scroll";
    tpLastX = (e.touches[0].clientX + e.touches[1].clientX) / 2;
    tpLastY = (e.touches[0].clientY + e.touches[1].clientY) / 2;
  } else {
    tpMode = "move";
    tpLastX = e.touches[0].clientX;
    tpLastY = e.touches[0].clientY;
  }
  tpStart = Date.now();
  tpMoved = 0;
}, { passive: false });

touchpad.addEventListener("touchmove", function (e) {
  e.preventDefault();
  var x = e.touches[0].clientX;
  var y = e.touches[0].clientY;
  if (e.touches.length >= 2) {
    x = (e.touches[0].clientX + e.touches[1].clientX) / 2;
    y = (e.touches[0].clientY + e.touches[1].clientY) / 2;
    if (tpMode !== "scroll") {
      tpMode = "scroll";
      tpLastX = x;
      tpLastY = y;
    }
  }
  var dx = x - tpLastX;
  var dy = y - tpLastY;
  tpLastX = x;
  tpLastY = y;
  tpMoved += Math.abs(dx) + Math.abs(dy);
  if (tpMode === "move") {
    tpDx += dx * TP_MOVE_SENSITIVITY;
    tpDy += dy * TP_MOVE_SENSITIVITY;
  } else if (tpMode === "scroll") {
    tpScrollAccum += dy;
  }
}, { passive: false });

touchpad.addEventListener("touchend", function (e) {
  if (e.touches.length === 0) {
    tpFlush();
    tpStopTimer();
    var duration = Date.now() - tpStart;
    if (!tpFlushed && tpMode === "move" && tpMaxTouch === 1 && tpMoved < TP_TAP_DISTANCE && duration < TP_TAP_TIME) {
      sendMouse({ click: true });
      hintFlash("左键");
    }
    tpMode = null;
    tpFlushed = false;
    tpMaxTouch = 0;
  } else if (e.touches.length === 1) {
    var duration2 = Date.now() - tpStart;
    if (tpMaxTouch === 2 && !tpFlushed && tpMoved < TP_TWO_TAP_DISTANCE && duration2 < TP_TWO_TAP_TIME) {
      sendMouse({ rightClick: true });
      hintFlash("右键");
    }
    tpMode = "move";
    tpLastX = e.touches[0].clientX;
    tpLastY = e.touches[0].clientY;
    tpStart = Date.now();
    tpMoved = TP_TAP_DISTANCE;
  }
}, { passive: false });

touchpad.addEventListener("touchcancel", function () {
  tpFlush();
  tpStopTimer();
  tpMode = null;
  tpFlushed = false;
  tpMaxTouch = 0;
}, { passive: false });

var scrollTrack = document.getElementById("scrollTrack");
var btnLeft = document.getElementById("btnLeft");
var btnRight = document.getElementById("btnRight");
var sbAccum = 0;
var sbLastY = 0;
var sbActive = false;
var SB_SCROLL_STEP = 8;

scrollTrack.addEventListener("touchstart", function (e) {
  e.preventDefault();
  sbActive = true;
  sbLastY = e.touches[0].clientY;
}, { passive: false });

scrollTrack.addEventListener("touchmove", function (e) {
  e.preventDefault();
  if (!sbActive) return;
  var y = e.touches[0].clientY;
  sbAccum += y - sbLastY;
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
    arrowTimer = setTimeout(repeat, 140);
  }, 380);
}

function stopArrowRepeat() {
  if (arrowTimer) {
    clearTimeout(arrowTimer);
    arrowTimer = null;
  }
}

scrollUp.addEventListener("touchstart", function (e) {
  e.preventDefault();
  startArrowRepeat(-1);
}, { passive: false });
scrollDown.addEventListener("touchstart", function (e) {
  e.preventDefault();
  startArrowRepeat(1);
}, { passive: false });
scrollUp.addEventListener("touchend", stopArrowRepeat);
scrollUp.addEventListener("touchcancel", stopArrowRepeat);
scrollDown.addEventListener("touchend", stopArrowRepeat);
scrollDown.addEventListener("touchcancel", stopArrowRepeat);

btnLeft.addEventListener("touchstart", function (e) {
  e.preventDefault();
  sendMouse({ click: true });
  hintFlash("左键");
}, { passive: false });

btnRight.addEventListener("touchstart", function (e) {
  e.preventDefault();
  sendMouse({ rightClick: true });
  hintFlash("右键");
}, { passive: false });

function updateStatus(text, cls) {
  statusEl.textContent = text;
  statusEl.className = cls || "";
}

function setDot(cls) {
  connDot.className = cls || "";
}

function checkConnection() {
  fetch("/ping").then(function (res) { return res.json(); }).then(function (data) {
    setDot("ok");
    pcName.textContent = data.hostname || "—";
  }).catch(function () {
    setDot("err");
    pcName.textContent = "—";
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
  form.append("pasteImage", imageToggle.checked ? "1" : "0");
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
      updateStatus("", "");
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

document.getElementById("menuBtn").addEventListener("click", openPanel);
document.getElementById("panelClose").addEventListener("click", closePanel);
panelMask.addEventListener("click", closePanel);
document.getElementById("inboxOpenBtn").addEventListener("click", openInbox);
document.getElementById("inboxClose").addEventListener("click", closeInbox);


sendBtn.addEventListener("click", sendText);
checkConnection();
setInterval(checkConnection, 10000);
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
    try:
        return pyperclip.paste().replace("\r\n", "\n") == text
    except Exception:
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
    print("鼠标：%s已注入（按下+抬起一次提交）" % label, flush=True)


def releaseAllButtons():
    if sendMouseEvent is None:
        return
    sendMouseEvent(MOUSEEVENTF_LEFTUP, quiet=True)
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


@app.get("/ping")
def ping():
    return jsonify(ok=True, hostname=socket.gethostname())


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
        if imagePaths:
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
    if not hasText:
        if imagePasted:
            return jsonify(ok=True, pasted=False, enterSent=False, saved=saved, imagePasted=imagePasted, message="图片已粘贴，原图已存入电脑收件箱")
        return jsonify(ok=True, pasted=False, enterSent=False, saved=saved, imagePasted=0, message="文件已存入电脑收件箱")
    if pasted:
        return jsonify(ok=True, pasted=True, enterSent=enterSent, saved=saved, imagePasted=imagePasted, message="已发送并粘贴到电脑光标处")
    return jsonify(ok=True, pasted=False, enterSent=False, saved=saved, imagePasted=imagePasted, message="文字已存入电脑剪贴板，可手动 Ctrl+V")


@app.get("/inbox")
def inboxList():
    items = []
    if os.path.isdir(ATTACH_DIR):
        for name in os.listdir(ATTACH_DIR):
            path = os.path.join(ATTACH_DIR, name)
            if os.path.isfile(path):
                stat = os.stat(path)
                items.append({"name": name, "size": stat.st_size, "mtime": int(stat.st_mtime)})
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


@app.post("/mouse")
def mouse():
    data = request.get_json(silent=True) or {}
    dx = int(data.get("dx") or 0)
    dy = int(data.get("dy") or 0)
    scroll = int(data.get("scroll") or 0)
    actions = []
    try:
        with mouseLock:
            releaseAllButtons()
            if dx or dy:
                pyautogui.moveRel(dx, dy)
                actions.append("移动(%d,%d)" % (dx, dy))
            if scroll:
                pyautogui.scroll(scroll)
                actions.append("滚轮%+d" % scroll)
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
        if actions and (data.get("click") or data.get("rightClick")):
            print("鼠标：%s" % " + ".join(actions), flush=True)
    except Exception as exc:
        print("鼠标操作失败：%r" % exc, flush=True)
        return jsonify(ok=False, message="鼠标操作失败：%s" % exc)
    return jsonify(ok=True)


def main():
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    try:
        releaseAllButtons()
    except Exception:
        pass
    ips = getLanIps()
    print("=" * 52)
    print("局域网文字投递工具 已启动")
    print("电脑名：%s" % socket.gethostname())
    print("手机浏览器访问以下地址（需与电脑同一 Wi-Fi）：")
    for ip in ips:
        print("  http://%s:%d" % (ip, PORT))
    print("=" * 52)
    print("发送时电脑当前激活窗口的光标位置即为粘贴目标")
    print("收到文件保存目录：%s" % ATTACH_DIR)
    print("首次运行若弹出防火墙提示，请勾选允许（专用网络）")
    print("按 Ctrl+C 停止服务")
    app.run(host=HOST, port=PORT, debug=False)


if __name__ == "__main__":
    main()