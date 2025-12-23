# Volcano Shelly Remote

One-button control for the **Storz & Bickel Volcano Hybrid** using a physical button,
Bluetooth LE and a local HTTP server.

This project turns the Volcano into a device that can be operated **blindly**,
without a phone, browser or touchscreen.

---

## What this is

This setup combines:

- A **Shelly BLU Button** (mounted directly on the hose)
- A **Shelly Plug (Gen 3)** to control power
- A **Python HTTP server** that talks to the Volcano via Bluetooth LE
- Desktop notifications and terminal output for live feedback

The result is a clean, tactile workflow:
press a button → heat up → wait → vape.

---

## Why this exists

The official Volcano web app works, but requires visual interaction and a browser.

This project focuses on:

- physical interaction instead of screens
- clear state feedback (heating, ready, vapor on/off)
- automation and reproducibility
- hackability for developers

---

## Typical workflow

- **Short press**
  - If the device is off: power on and heat to last temperature
  - If ready: toggle fan (vapor on / off)

- **Long press**
  - Power everything down

The PC shows:
- connection status
- temperature progress
- readiness via desktop notifications
- clean, readable terminal output

---

## Repository structure

```text
Server/
  volcano_http.py     # BLE → HTTP server for the Volcano Hybrid
  volcano_icons.py    # Dynamic temperature-based notification icons

Shelly/
  blu-button.js       # Shelly BLU Button script (button → HTTP mapping)
