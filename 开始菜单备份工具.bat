@echo off
chcp 65001 >nul
title Windows 11 开始菜单布局备份恢复工具
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0StartMenuBackup.ps1"