[app]
title = Candle Predictor
package.name = candlebot
package.domain = org.nirob

source.dir = .
source.include_exts = py,png,jpg,kv,atlas,mp3,mp4
source.exclude_dirs = bin,venv,.buildozer,__pycache__

version = 1.0

# openssl+certifi are needed for wss:// and https:// on Android.
# pyjnius is required by plyer's Android TTS.
requirements = python3,kivy==2.1.0,requests,websockets,plyer

orientation = portrait
fullscreen = 0
presplash.color = #05080F

android.permissions = INTERNET,ACCESS_NETWORK_STATE,WAKE_LOCK
android.api = 33
android.minapi = 24
android.ndk = 25b
android.archs = arm64-v8a
android.accept_sdk_license = True
android.allow_backup = True
android.debug_artifact = apk
android.logcat_filters = *:S python:D

p4a.bootstrap = sdl2

[buildozer]
log_level = 2
warn_on_root = 1
