# Changelog

## 1.3.1
- Fix: curve panel crashed with `min() arg is an empty sequence` when a data source (e.g. GPU) had no samples inside the visible window. Render errors are now also written to `error.log`.

## 1.3.0
- Windows agent: when a Windows PC reports in, the NAS screen switches to the Windows overview and back to the NAS view after it goes away.

## 1.2.0
- Intel iGPU support (i915 PMU, RC6 fallback), CPU/GPU usage & temperature curves, always dark background.

## 1.1.0
- Disks (SMART), fans, temperatures, processes, Docker, two-column layout.

## 1.0.0
- First version: NAS overview on the local console (tty1).
