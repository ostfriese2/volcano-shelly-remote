# Volcano Shelly Remote

One-button control for the **Storz & Bickel Volcano (Hybrid)** using a physical button,
Bluetooth LE and a local HTTP server.

This project turns the Volcano into a device that can be operated **blindly**,
without a phone, browser or touchscreen.

---

## What this is

This setup combines:

- A **Shelly BLU Button** (mounted directly on the hose if wanted)
- A **Shelly Plug (Gen 2/3)** to control power
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
## Developer Notes

This project intentionally contains more Bluetooth LE information than strictly required
for the basic one-button use case.

### BLE characteristics

All known Bluetooth LE characteristics used by the Volcano Hybrid are defined in the code.
These values were obtained by observing the device’s normal BLE communication.

- All characteristics that are actively used are considered stable
- Additional characteristics are included for experimentation and further development
- Experimental or unverified characteristics are clearly marked in the code

Nothing here is encrypted or bypassed — the device exposes these characteristics openly
and they can be read or written using standard BLE tooling.

### Developer mode

When started with:
```bash
python3 volcano_http.py --devmode
```

```bash


## Repository structure

```text
server/
  volcano_http.py     # BLE → HTTP server for the Volcano Hybrid
  volcano_icons.py    # Dynamic temperature-based notification icons

shelly/
  blu-button.js       # Shelly BLU Button script (button → HTTP mapping)
```


## Disclaimer

This project is not affiliated with, endorsed by, or supported by
Storz & Bickel GmbH.

All communication with the device uses standard, publicly accessible
Bluetooth LE characteristics as exposed by the Volcano Hybrid.

No firmware is modified and no security mechanisms are bypassed.

If you are a representative of Storz & Bickel and have concerns about this project,
please feel free to get in touch via the contact address provided.
