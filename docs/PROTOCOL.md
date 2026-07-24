# SONOFF SWV — protocol reference

Technical detail behind the quirk: the device's endpoint and clusters, the
manufacturer-specific attributes, how the irrigation programs are encoded, and worked
examples for reading and writing them.

This is what you need if you want to hack on the quirk, port it, or drive the valve by
hand. For installing and using it, see the [README](../README.md).

Everything here was captured from real hardware (a ZHA diagnostics dump plus live
reads/writes), firmware version `0x00001004` (1.0.4). Other firmware may differ.

---

## Device

| | |
|---|---|
| Manufacturer | `SONOFF` |
| Model | `SWV` |
| Manufacturer code | `4742` (`0x1286`) |
| Logical type | End device (sleepy — `rx_on_when_idle` = 0) |
| Power | Battery |

Being a sleepy end device matters: it does not receive continuously. Commands are
queued by the coordinator until the valve next polls in, so a burst of writes fired
back-to-back can be dropped. Space them out.

## Endpoint 1

Profile `0x0104` (Home Automation), device type `0x0002` (`ON_OFF_OUTPUT`). This is the
only endpoint.

### Input (server) clusters

| Cluster | ID | What it is |
|---|---|---|
| Basic | `0x0000` | `manufacturer` = "SONOFF", `model` = "SWV" |
| Power Configuration | `0x0001` | battery — see below |
| Identify | `0x0003` | standard |
| On/Off | `0x0006` | the valve open/close switch (`on_off` `0x0000`) |
| Poll Control | `0x0020` | `fast_poll_timeout` `0x0003` |
| Flow Measurement | `0x0404` | live water flow — see below |
| Diagnostics | `0x0B05` | standard |
| **eWeLink (manufacturer)** | `0xFC11` | **all the irrigation features — see below** |
| Works-With-All-Hubs | `0xFC57` | SONOFF interop cluster, unused here |

### Output (client) clusters

| Cluster | ID | What it is |
|---|---|---|
| Time | `0x000A` | the valve reads the coordinator's clock from here |
| OTA | `0x0019` | firmware updates (`current_file_version` = `0x1004`) |

### Battery (`0x0001`)

| Attribute | ID | Notes |
|---|---|---|
| `battery_voltage` | `0x0020` | units of 0.1 V — raw `59` = 5.9 V |
| `battery_percentage_remaining` | `0x0021` | ZCL half-percent — raw `180` = 90 % |

Handled by ZHA's standard battery sensor; the quirk does not touch it.

### Flow (`0x0404`)

| Attribute | ID | Notes |
|---|---|---|
| `measured_value` | `0x0000` | uint16, ZCL "10 × flow in m³/h" — raw `3` = 0.3 m³/h |

Standard ZCL flow measurement, exposed by ZHA's built-in flow sensor. Observed
0.1–0.3 m³/h while watering, reported sparsely and often `0` between updates. The quirk
does not touch this cluster either.

---

## The eWeLink cluster (`0xFC11`)

This is where every irrigation feature lives. All attributes below are **not**
manufacturer-specific (they are read/written with no manufacturer code), despite living
in a manufacturer cluster.

| Attribute | ID | Type | Access | Meaning |
|---|---|---|---|---|
| `real_time_irrigation_duration` | `0x5006` | uint32 | R | Duration of the current/last session, **seconds** |
| `real_time_irrigation_volume` | `0x5007` | uint32 | R | Volume of the current/last session, **whole litres** |
| `cyclic_timed_irrigation` | `0x5008` | string | R/W | Timed program (water for N seconds) — see encoding |
| `cyclic_quantitative_irrigation` | `0x5009` | string | R/W | Volume program (water until N litres) — see encoding |
| `water_valve_state` | `0x500C` | enum8 | R | Bit 0 = water shortage, bit 1 = water leakage |
| `irrigation_start_time` | `0x500D` | uint32 | R | Start of last session — **local wall clock**, see below |
| `irrigation_end_time` | `0x500E` | uint32 | R | End of last session — **local wall clock** |
| `daily_irrigation_volume` | `0x500F` | uint32 | R | Litres today, resets daily |
| `valve_work_state` | `0x5010` | bool | R | A cyclic program is running |
| `auto_close_water_shortage` | `0x5011` | uint16 | R/W | Auto-close-on-shortage timeout, minutes (`0` = off, `30` = on) |

### `water_valve_state` (`0x500C`) is a bitfield

| Value | Shortage | Leakage |
|---|---|---|
| `0` | no | no |
| `1` | **yes** | no |
| `2` | no | **yes** |
| `3` | **yes** | **yes** |

The quirk splits it into two binary sensors: leakage = `value & 2`, shortage =
`value & 1`.

---

## Irrigation program encoding (`0x5008` / `0x5009`)

Both program attributes are carried as a ZCL **character string** (type `0x42`) whose
bytes are actually a binary struct — not text. Decoding it as a string mangles it (it
stops at the first `0x00`), so the raw bytes must be read directly.

### Layout — 10 bytes

| Offset | Bytes | Field | Notes |
|---|---|---|---|
| 0 | 1 | `cycles_done` | how many cycles have completed (device-owned; write as 0) |
| 1 | 1 | `cycles_total` | number of cycles to run |
| 2 | 4 | `amount` | **big-endian**. Seconds (`0x5008`) or litres (`0x5009`) |
| 6 | 4 | `interval` | **big-endian**. Seconds to wait between cycles |

On the wire the attribute value is the ZCL string form: a `0x0A` length byte followed by
these 10 bytes.

### Worked example — build a program

"Water once for 10 seconds" (timed, `0x5008`):

```
cycles_done  = 0
cycles_total = 1
amount       = 10   -> 0x0000000A
interval     = 1    -> 0x00000001   (see "every field must be non-zero" below)

struct  = 00 01 00 00 00 0A 00 00 00 01     (10 bytes)
on wire = 0A 00 01 00 00 00 0A 00 00 00 01  (length-prefixed string)
```

### Worked example — decode a report

The device reports `0x5008` as `0A 02 03 00 00 04 B0 00 00 0E 10`:

```
0A                 string length = 10
   02              cycles_done  = 2
   03              cycles_total = 3
   00 00 04 B0     amount       = 1200  (seconds)
   00 00 0E 10     interval     = 3600  (seconds)
```

→ "on cycle 2 of 3, 1200 s each, 3600 s apart".

### The short 2-byte form

When a program finishes, the device reports a **2-byte** value — just `cycles_done` and
`cycles_total`, no amount or interval:

```
02 01 01   ->  length 2, cycles_done = 1, cycles_total = 1
```

So a reader must keep the last known amount/interval rather than treating the missing
fields as zero. The quirk does this.

---

## Behaviour you must design around

Measured, not documented anywhere. These three facts drive the quirk's whole shape:

1. **Writing a program starts the watering.** The instant `0x5008` or `0x5009` is
   written, the valve opens and runs that program — there is no separate "turn on"
   step, and the On/Off switch (`0x0006`) is not involved. The valve closes itself when
   the program completes.

2. **Every field must be non-zero.** A program containing a zero in `cycles_total`,
   `amount`, or `interval` is rejected with ZCL status `INVALID_VALUE` (`0x87`) — even
   the interval, which does nothing on a single-cycle run. Because a rejected write is a
   watering that silently never happens, the quirk floors all three at 1.

   > This was the source of a long false trail during development. The failures looked
   > like a byte-format problem; they were actually zero-valued fields. If you write
   > directly, make sure no field is zero.

3. **Timestamps are local wall clock, not UTC.** `0x500D`/`0x500E` are Unix-epoch
   seconds, but the valve fills them from the Time cluster's `localTime`, so the number
   already carries your UTC offset. Convert by reading it as naive UTC, then stamping
   your local zone. Example: a session that really started at `11:00:20 UTC` (which is
   `21:00:20` local, +10) was reported as `1784754020` — which reads as `21:00:20 UTC`.
   Re-stamping that wall clock as local time gives back `11:00:20 UTC`, within 1 s of the
   real event.

---

## Quirk-internal virtual attributes

These do **not** exist on the device. The quirk invents them so the packed programs can
be exposed as ordinary Home Assistant number and button entities. Reads and writes of
these are translated to/from the real packed attribute in the cluster; they never go on
the air by themselves.

| Virtual ID | Maps to | Field | Entity |
|---|---|---|---|
| `0xFF00` | `0x5008` | cycles_done | Timed cycles completed (sensor) |
| `0xFF01` | `0x5008` | cycles_total | Timed cycles (number) |
| `0xFF02` | `0x5008` | amount | Timed duration (number) |
| `0xFF03` | `0x5008` | interval | Timed interval (number) |
| `0xFF04` | `0x5009` | cycles_done | Quantitative cycles completed (sensor) |
| `0xFF05` | `0x5009` | cycles_total | Quantitative cycles (number) |
| `0xFF06` | `0x5009` | amount | Quantitative volume (number) |
| `0xFF07` | `0x5009` | interval | Quantitative interval (number) |
| `0xFF08` | commits `0x5008` | — | Start timed irrigation (button) |
| `0xFF09` | commits `0x5009` | — | Start quantitative irrigation (button) |

**How the staging works.** Writing `0xFF00`–`0xFF07` only updates the cluster cache —
no Zigbee traffic. Pressing a start button (`0xFF08`/`0xFF09`) reads the staged fields,
packs them into the 10-byte struct, and writes it once to the real `0x5008`/`0x5009` —
which is the write that actually starts the watering. This is why setting the numbers
does nothing until you press Start, and why you never fire a half-built program.

---

## Examples

### Read the valve state (Developer Tools → Template)

```jinja
{{ states('binary_sensor.YOUR_SWV_water_leak') }}
{{ states('sensor.YOUR_SWV_daily_irrigation_volume') }}
{{ states('sensor.YOUR_SWV_irrigation_start_time') }}
```

### Drive it through the quirk's entities (the normal way)

```yaml
# Stage a one-shot 10-minute timed program, then start it.
- action: number.set_value
  target: { entity_id: number.YOUR_SWV_timed_irrigation_cycles }
  data: { value: 1 }
- action: number.set_value
  target: { entity_id: number.YOUR_SWV_timed_irrigation_interval }
  data: { value: 1 }          # must not be 0
- action: number.set_value
  target: { entity_id: number.YOUR_SWV_timed_irrigation_duration }
  data: { value: 600 }
- action: button.press          # <-- this is what opens the valve
  target: { entity_id: button.YOUR_SWV_start_timed_irrigation }
```

### Read the raw attribute (zha_toolkit)

```yaml
- action: zha_toolkit.attr_read
  data:
    ieee: "xx:xx:xx:xx:xx:xx:xx:xx"
    endpoint: 1
    cluster: 0xFC11
    attribute: 0x5008
    use_cache: false
```

### Write a raw program (zha_toolkit)

Bypasses the quirk entirely — sends the 10 bytes as a character string. This **starts a
watering immediately**. `attr_val` is the 10 struct bytes (zha_toolkit adds the string
length prefix); "water once for 10 s, 60 s interval":

```yaml
- action: zha_toolkit.attr_write
  data:
    ieee: "xx:xx:xx:xx:xx:xx:xx:xx"
    endpoint: 1
    cluster: 0xFC11
    attribute: 0x5008
    attr_type: 0x42          # character string
    attr_val: [0, 1, 0, 0, 0, 10, 0, 0, 0, 60]
```

---

## Credits

Attribute IDs and the program layout originate in the
[Zigbee2MQTT SONOFF converter](https://github.com/Koenkk/zigbee-herdsman-converters).
The behavioural notes above (start-on-write, the non-zero rule, the local-time
timestamps, the short-form report) were established by testing this hardware directly.
