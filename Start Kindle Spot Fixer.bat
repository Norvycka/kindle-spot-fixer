@echo off
cd /d "%~dp0"
python -m pip install --quiet reportlab beautifulsoup4 lxml
python kindle_spot.py
pause
