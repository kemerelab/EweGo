# UBX Validation Guide

## How to Validate Your Logged Data

### Step 1: Run the Validator

```bash
python validate_ubx.py gps_log_20260105_142315.ubx
```

### Step 2: Interpret the Results

## ✅ What You NEED for PPK (Critical Requirements)

### 1. RXM-RAWX Messages (Raw Observations)
- **Required:** YES - This is the actual satellite observation data
- **Expected rate:** ~5 Hz (should match your configured rate)
- **What it contains:** Pseudorange, carrier phase, Doppler measurements
- **If missing:** PPK is IMPOSSIBLE - reconfigure u-center and re-record

### 2. RXM-SFRBX Messages (Ephemeris Data)
- **Required:** YES - Contains satellite orbit information
- **Expected rate:** Variable (as broadcast by satellites, ~every 30 seconds per satellite)
- **What it contains:** Satellite position/clock information
- **If missing:** PPK is IMPOSSIBLE - reconfigure u-center and re-record

### 3. NAV-PVT Messages (Position/Velocity/Time)
- **Required:** NO for PPK, but useful for validation
- **Expected rate:** ~5 Hz
- **What it contains:** Real-time position solution from GPS
- **Use:** Verify GPS was working and getting fixes during recording

---

## 📊 What to Look For in the Report

### Time Span
```
Time Span:
  Start:    2026-01-05 14:23:15
  End:      2026-01-05 15:45:32
  Duration: 1:22:17 (4937.0 seconds)
```

✅ **Good:** Duration matches your recording session  
⚠️ **Warning:** If much shorter than expected, check for recording interruptions

### Critical Messages
```
Critical Messages for PPK:
  ✓ RXM-RAWX (raw observations):  49,370 messages (10.0 Hz)
  ✓ RXM-SFRBX (ephemeris):        1,245 messages
```

✅ **Good:** Both present, RXM-RAWX at ~5 Hz  
⚠️ **Warning:** RXM-RAWX rate below 4.75 Hz  
✗ **Bad:** Either message type missing or at 0 Hz

### Fix Type Distribution
```
Fix Type Distribution:
  NO FIX              :   245 (  0.5%)
  3D FIX              : 48,125 ( 97.5%)
  TIME ONLY           :  1,000 (  2.0%)
```

✅ **Good:** Mostly 3D fixes (>90%)  
⚠️ **Warning:** >10% NO FIX or 2D FIX  
✗ **Bad:** Majority NO FIX (GPS didn't work properly)

**Note:** For PPK, the real-time fix type doesn't matter much - you're post-processing anyway. But it's a good indicator that the GPS was working.

### RTK Carrier Solution (if NTRIP was used)
```
RTK Carrier Solution Distribution:
  No carrier          : 15,000 ( 30.4%)
  Float solution      : 20,000 ( 40.5%)
  Fixed solution      : 14,370 ( 29.1%)
```

✅ **Great:** Significant percentage of Fixed or Float (means NTRIP corrections were received)  
⚠️ **OK:** All "No carrier" (means NTRIP wasn't used or not working, but raw data still good for PPK)  
✗ **Bad:** This doesn't affect PPK at all - it's just real-time RTK status

### Satellite Statistics
```
Satellite Statistics:
  Max satellites observed: 18
```

✅ **Good:** 12+ satellites  
⚠️ **Warning:** 6-11 satellites (PPK may work but accuracy reduced)  
✗ **Bad:** <6 satellites (insufficient for PPK)

---

## 🎯 Example: Good Data

```
VALIDATION REPORT
=================

Time Span:
  Duration: 1:30:00 (5400.0 seconds)

Critical Messages for PPK:
  ✓ RXM-RAWX (raw observations):  54,000 messages (10.0 Hz) ✅
  ✓ RXM-SFRBX (ephemeris):        1,350 messages ✅

Fix Type Distribution:
  3D FIX              : 52,000 ( 96.3%) ✅

Satellite Statistics:
  Max satellites observed: 16 ✅

✓ VALIDATION PASSED: File appears suitable for PPK processing
```

---

## 🚫 Example: Bad Data (Missing Raw Observations)

```
VALIDATION REPORT
=================

Critical Messages for PPK:
  ✓ RXM-RAWX (raw observations):  0 messages ✗ MISSING!
  ✓ RXM-SFRBX (ephemeris):        1,350 messages

✗ ERRORS:
  - No RXM-RAWX messages found! These are REQUIRED for PPK processing.

✗ VALIDATION FAILED: File is NOT suitable for PPK processing
  Please check your u-center configuration and re-record data.
```

**Fix:** Go back to u-center 2 and enable RXM-RAWX messages at rate=1

---

## 🚫 Example: Bad Data (Missing Ephemeris)

```
VALIDATION REPORT
=================

Critical Messages for PPK:
  ✓ RXM-RAWX (raw observations):  54,000 messages (10.0 Hz)
  ✓ RXM-SFRBX (ephemeris):        0 messages ✗ MISSING!

✗ ERRORS:
  - No RXM-SFRBX messages found! These contain ephemeris data REQUIRED for PPK.

✗ VALIDATION FAILED: File is NOT suitable for PPK processing
```

**Fix:** Go back to u-center 2 and enable RXM-SFRBX messages at rate=1

---

## ⚠️ Example: Marginal Data (Low Rate)

```
VALIDATION REPORT
=================

Critical Messages for PPK:
  ✓ RXM-RAWX (raw observations):  25,000 messages (4.6 Hz)
  ✓ RXM-SFRBX (ephemeris):        1,200 messages

⚠ WARNINGS:
  - RXM-RAWX rate is 2.3 Hz (expected ~5 Hz)

✓ VALIDATION PASSED: File appears suitable for PPK processing
  Some warnings were found - review above for details.
```

**Analysis:** Data is usable but at lower rate than configured. Possible causes:
- Baud rate too low (increase to 230400)
- Serial buffer overflow
- NMEA output enabled (disable it)

---

## 📋 Quick Validation Checklist

Before attempting PPK processing, verify:

- [ ] **File size reasonable** (25-80 MB per hour at 5 Hz, depending on satellites tracked)
- [ ] **RXM-RAWX present** and at ~5 Hz
- [ ] **RXM-SFRBX present** (at least 500+ messages for 1 hour)
- [ ] **NAV-PVT shows 3D fixes** for most of recording
- [ ] **Satellites observed** (12+ is great, 6-11 is OK, <6 is bad)
- [ ] **Duration matches** your recording session

---

## 🔧 Common Issues and Fixes

### Issue: No RXM-RAWX or RXM-SFRBX messages

**Cause:** Not enabled in u-center configuration  
**Fix:** 
1. Open u-center 2
2. Go to Configuration → Message Enabler
3. Enable RXM-RAWX and RXM-SFRBX at rate=1 on UART1
4. Save configuration to Flash
5. Re-record data

### Issue: Low message rate (<9 Hz)

**Cause:** Baud rate too low or NMEA output enabled  
**Fix:**
1. Increase baud rate to 230400
2. Disable NMEA output protocol
3. Verify with validate script after re-recording

### Issue: Low satellite count (<6)

**Cause:** Poor sky view  
**Fix:**
1. Move GPS antenna outdoors or near window
2. Ensure clear view of sky (no buildings/trees blocking)
3. Wait longer for satellite acquisition
4. Re-record data

### Issue: File is empty or very small

**Cause:** Logging script didn't run properly  
**Fix:**
1. Check serial port is correct
2. Verify baud rate matches u-center config
3. Ensure no other program (like u-center) has serial port open
4. Check for error messages in logging script output

---

## 📦 What's Actually Needed for PPK?

PPK processing requires:

1. **Your rover data** (this is what you're logging)
   - Raw observations (RXM-RAWX)
   - Ephemeris (RXM-SFRBX)
   
2. **Base station data** (usually from CORS or your own base)
   - Raw observations from a known location
   - Can be in RINEX format
   
3. **Processing software** (RTKLIB or similar)
   - Combines rover + base data
   - Calculates precise positions

The validation script only checks #1 (your rover data). You'll need to separately obtain base station data for your recording time period.

---

## Next Steps After Validation

If validation passes:

1. **Convert to RINEX:**
   ```bash
   convbin -r ubx -o rinex -od -os -v 3.04 gps_log_20260105_142315.ubx
   ```

2. **Obtain base station data:**
   - Download RINEX from nearby CORS station
   - Or use your own base station data
   - Must cover same time period as rover data

3. **Process with RTKLIB:**
   ```bash
   rnx2rtkp rover.obs base.obs nav_file
   ```

4. **Analyze results:**
   - Look for Q=1 (fixed solution) or Q=2 (float solution)
   - Q=5 means single point positioning (no PPK benefit)
