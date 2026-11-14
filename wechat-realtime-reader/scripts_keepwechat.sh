#!/bin/bash
pgrep -f "/Applications/WeChat.app/Contents/MacOS/WeChat" >/dev/null 2>&1 || open -a /Applications/WeChat.app
