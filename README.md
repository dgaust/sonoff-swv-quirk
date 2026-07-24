# SONOFF SWV — better water valve control for Home Assistant

If you use a SONOFF SWV smart water valve with Home Assistant's ZHA integration, you've
probably noticed it only shows a handful of things — an on/off switch, water leak, and
not much else. All the irrigation features you get in the SONOFF app or with
Zigbee2MQTT are missing.

This project fixes that. It's two parts:

1. **A quirk** — a small file that teaches ZHA about the valve's hidden features, so
   they show up as normal Home Assistant entities.
2. **A blueprint** — an optional ready-made automation that waters a garden zone and
   lets *the valve itself* handle the timing.

You can use just the quirk if you like. The blueprint is a bonus.

## Why let the valve do the timing?

Normally, to water for 5 minutes, Home Assistant turns the valve on, waits 5 minutes,
then turns it off. If Home Assistant restarts or loses connection during those 5
minutes, the valve never gets the "off" command — and your garden floods.

The SWV can run a timer on its own. You tell it "water for 5 minutes" and it counts
down and shuts off by itself, even if Home Assistant goes away completely. That's what
this project unlocks, and what the blueprint is built around.

## What you get

The quirk adds these entities to the valve:

- **How much it's watering** — current/last watering duration and volume, plus a
  running daily total.
- **When it watered** — start and end time of the last session.
- **Whether a program is running right now** — a simple on/off sensor.
- **Two watering programs you can set up** — one by *time* (water for N seconds) and
  one by *volume* (water until N litres), each with a **Start** button.

Your existing entities keep their names — the leak sensor, the switch, and so on all
stay exactly as they were.

## Installing the quirk

1. Create a folder for custom quirks if you don't have one, e.g. `/config/custom_zha_quirks/`.
   (Home Assistant won't start if you point it at a folder that doesn't exist, so make
   it first.)
2. Copy `custom_zha_quirks/sonoff_swv.py` into that folder.
3. Tell ZHA where to look: **Settings → Devices & services → Zigbee Home Automation →
   Configure → Custom quirks path**, or add this to `configuration.yaml`:

   ```yaml
   zha:
     custom_quirks_path: /config/custom_zha_quirks/
   ```
4. Restart Home Assistant. Then open the valve's device page and click **Reconfigure**
   so it picks up the new features.

You'll see a line in the log saying *"Loaded custom quirks…"* — that's just Home
Assistant confirming it worked, not an error.

## Using the blueprint (optional)

Import it into Home Assistant from:

```
https://github.com/dgaust/sonoff-swv-quirk/blob/main/blueprints/automation/smart_irrigation_swv.yaml
```

(Settings → Automations & Scenes → Blueprints → Import Blueprint.)

It works with the [Smart Irrigation](https://github.com/jeroenterheerdt/HASmartIrrigation)
integration, which calculates how long each zone needs to be watered. When Smart
Irrigation says "go", the blueprint tells the valve how long to water and lets it run
on its own timer.

You only need to pick **two things**: the valve and the zone. Everything else about the
valve is found automatically.

Extra options if you want them:

- **Pause switch** — turn watering off for rain, holidays, or maintenance.
- **Maximum watering time** — a safety cap, in case something asks for a silly duration.
- **Failsafe** — Home Assistant keeps an eye on the valve and shuts it off manually if
  it somehow doesn't close on its own. (It normally does.)
- **Notification** — get a message on your phone when watering starts, telling you
  which valve and for how long: *"Rear Garden Valve is watering for 5 min 12 s."*

## A few quirks of the valve worth knowing

These aren't bugs — they're just how the hardware behaves, and they can be surprising:

- **Setting a program starts it.** There's no separate "on" step. That's exactly why the
  numbers work the way they do: you set them (nothing happens yet), then press the
  **Start** button to actually begin watering.
- **The volume sensor only counts whole litres.** A very short test run of under a litre
  will show `0` litres, even though the daily total ticks up. Real waterings are well
  over a litre, so this only shows up during quick tests.
- **Times are in your local time zone**, and the quirk handles that for you.

## Has this actually been tested?

Yes, on real hardware (two valves):

- Setting the numbers does nothing until you press Start — confirmed.
- Pressing Start opens the valve within a few seconds.
- A 10-second program ran for exactly 10 seconds and closed itself — three times over.
- The times and volumes reported match what actually happened.

The one thing not fully tested is the *volume* program stopping exactly at its target
(the test was stopped by hand before it got there). The blueprint uses the *time*
program, which is fully proven.

## For developers

If you want to hack on the quirk, port it, or drive the valve by hand, see
[docs/PROTOCOL.md](docs/PROTOCOL.md) — the endpoint and clusters, every
manufacturer-specific attribute, how the irrigation programs are encoded, and worked
read/write examples.

## Thanks

The valve's hidden features were mapped out by the
[Zigbee2MQTT](https://github.com/Koenkk/zigbee-herdsman-converters) project, and this
builds on the standard quirk from
[zha-device-handlers](https://github.com/zigpy/zha-device-handlers).

## Licence

MIT — see [LICENSE](LICENSE).
