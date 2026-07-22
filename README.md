# SONOFF SWV — ZHA quirk + Smart Irrigation blueprint

A [ZHA](https://www.home-assistant.io/integrations/zha) custom quirk for the **SONOFF
SWV** Zigbee smart water valve, exposing the irrigation features the Zigbee2MQTT
converter has but ZHA does not — and a Home Assistant blueprint that waters a
[Smart Irrigation](https://github.com/jeroenterheerdt/HASmartIrrigation) zone using the
valve's *own* timer, so the watering does not depend on Home Assistant staying up to
turn the tap off.

The stock `zhaquirks.sonoff.swv` quirk gives you three entities: water leak, water
supply, and the shortage auto-close switch. This adds sixteen more.

## Entities

| Entity | Attribute | Notes |
|---|---|---|
| Irrigation program running | `0x5010` | on while a cyclic program runs |
| Irrigation duration | `0x5006` | current/last session, seconds |
| Irrigation volume | `0x5007` | current/last session, **whole litres** |
| Daily irrigation volume | `0x500F` | resets daily, `total_increasing` |
| Irrigation start / end time | `0x500D` / `0x500E` | timestamps, see below |
| Timed irrigation cycles / duration / interval | `0x5008` | staged settings |
| Quantitative irrigation cycles / volume / interval | `0x5009` | staged settings |
| Timed / Quantitative cycles completed | | progress through a program |
| **Start timed irrigation** (button) | | commits and starts the timed program |
| **Start quantitative irrigation** (button) | | commits and starts the volume program |

Plus the three from the stock quirk, re-declared with their original unique-id suffixes
and translation keys so **existing entity IDs and names survive** the swap.

## What this firmware actually does

Measured against real hardware (firmware `0x1004`), not inferred from the Z2M source.
These were all surprises, and they are why the quirk is shaped the way it is:

- **Writing a program starts the watering.** There is no arm-then-switch-on: the valve
  opens the moment `0x5008`/`0x5009` lands and closes itself when the program finishes.
  That is the whole point — device-side timing that survives a Home Assistant restart.
- **Every field must be non-zero**, or the write is refused with `INVALID_VALUE` — even
  the interval, which does nothing on a single-cycle run. The numbers are floored at 1.
- Because a write *starts* a run, the six numbers do **not** write through. They stage
  values in the cluster's cache and a start button commits them as one write. Writing
  them individually would kick off a watering per field, each with a half-built program.
- The program is 10 bytes — `done:u8, total:u8, amount:u32 BE, interval:u32 BE` — in a
  ZCL character string. After a program completes the device reports a short **2-byte**
  form (done and total only), so the last known duration and interval are kept locally.
- **Session timestamps are local wall clock**, not UTC, despite the 1970 epoch: the
  valve stores the Time cluster's `localTime` verbatim. The quirk converts them back.
- `irrigation_volume` is whole litres, so a run under a litre reports `0` while the
  daily total still advances. That is the device, not a rounding bug in the quirk.

## Install

1. Copy `custom_zha_quirks/sonoff_swv.py` into your ZHA custom quirks directory —
   by convention `/config/custom_zha_quirks/`. **Create the directory first**; Home
   Assistant refuses to start if the configured path does not exist.
2. Point ZHA at it: *Settings → Devices & services → Zigbee Home Automation →
   Configure → Custom quirks path*, or in `configuration.yaml`:

   ```yaml
   zha:
     custom_quirks_path: /config/custom_zha_quirks/
   ```
3. Restart Home Assistant, then reconfigure the valve (device page → *Reconfigure*) so
   the new attributes are read.

Custom quirks are registered after the built-in ones and matched first, so this
replaces the stock quirk wholesale. You should see `Loaded custom quirks…` in the log —
that warning is the confirmation it worked.

## Blueprint

`blueprints/automation/smart_irrigation_swv.yaml` — one automation per zone.

On a Smart Irrigation start trigger it stages a one-shot program (1 cycle, duration =
the seconds Smart Irrigation calculated), presses **Start timed irrigation**, and resets
the zone's bucket. The valve runs the program and shuts itself off. There is no
`switch.turn_on` and no `delay`.

You pick the **valve device** and the **zone sensor**; the switch, the three numbers and
the start button are found from the device. If they cannot be found the automation stops
with a notification rather than watering blindly.

Options: a pause switch, a maximum watering time, and a failsafe that waits for the
valve to close and forces it shut if it has not — inert unless the device-side program
fails.

## Verified

On two SWV valves via the ZHA/HA APIs:

- staging the numbers produces **zero** Zigbee traffic and never opens the valve;
- the start button writes once and the valve opens within a few seconds;
- a 10-second program ran for exactly 10 seconds and closed itself, three times over;
- session timestamps land within 1s of when HA recorded the valve opening;
- the quantitative program starts and `irrigation_volume` tracks litres as it runs.

Not verified: that the quantitative program stops at its target volume — the run was cut
short by hand before it got there. The blueprint does not use that path.

## Credits

Attribute IDs and the program layout come from the
[zigbee-herdsman-converters](https://github.com/Koenkk/zigbee-herdsman-converters)
SONOFF definition. The stock quirk this builds on lives in
[zha-device-handlers](https://github.com/zigpy/zha-device-handlers).

## Licence

MIT — see [LICENSE](LICENSE).
