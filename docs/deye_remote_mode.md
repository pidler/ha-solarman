# Deye P3 Remote Mode: phase 1

Base: davidrapan/ha-solarman `ac1d88b83268beeb0511b8a1b7fc8e17deddc044`.
No automation, activation, initialization writes or physical-device writes were
performed. Only entity definitions and value validation are implemented.

## Evidence and scope

- [Deye three-phase MODBUS RTU protocol, distributor-hosted original](https://download.axilogi.com/DEYE/Other/MODBUS_RTU.pdf):
  revision history through 2025-01-13, printed pages 41-42 for remote registers;
  page 3 specifies 03H/10H and high byte first. Its introductory address ranges
  are inconsistent with the later remote table explicitly marked R/W.
- [Protocol linked by the upstream wiki](https://github.com/user-attachments/files/22990113/MODBUS.RTU.V105.4-20240814.pdf):
  downloaded and inspected, same remote table on pages 41-42.
- The requester reports a newer public V105.4 revision with history through
  2025-10-22 and confirms 1109 as signed 16-bit, 0.1% of rated power, and 1121 as
  phase-C reactive setpoint. The URL/file of that newer revision was not provided;
  this confirmation is requester-supplied evidence, not an independently inspected
  firmware-specific vendor statement. Attach that revision before upstream review.
- [Upstream PR #978](https://github.com/davidrapan/ha-solarman/pull/978/files),
  commit `61d71a1`, was reviewed but not applied. In particular its voltage/current
  addresses are shifted, SOC is assigned to 1108 instead of 1110 without the
  required scale, and it calls 1121 status. It also inserts a group before the
  trailing energy attributes of an existing entity. This change inserts a complete
  group between existing groups and preserves all other profile content.

| Register | Entity | Raw format / range | Display |
| --- | --- | --- | --- |
| 1100 / 0x044C | Remote Mode | unsigned enum 0-3 | Disabled / mode 1 / mode 2 / mode 3 |
| 1101 / 0x044D | Remote Watchdog | unsigned 10-18000 | seconds |
| 1101 / 0x044D | Remote Watchdog Status | read-only enum | Enabled / Disabled |
| 1101 / 0x044D | Disable Remote Watchdog button | unsigned 65535 | disables watchdog |
| 1104 / 0x0450 | Remote Power Control Mode | unsigned enum 0-1 | AC-side / Battery-side |
| 1105 / 0x0451 | Remote Battery-side Control Strategy | unsigned enum 2 | Power |
| 1109 / 0x0455 | Remote Constant Power Setpoint | signed 16-bit -1200..1200 | -120.0..120.0%, step 0.1% |
| 1110 / 0x0456 | Remote Constant SOC Setpoint | unsigned 0..1000 | 0.0..100.0%, step 0.1% |

The vendor table's range and notes disagree for 1104 (range 0-1, notes also
describe Grid=2) and 1105 (range 0-3, notes also describe 4/5; 0/1 reserved).
This first change exposes AC-side/Battery-side and Power (2) only. There is no
synthetic default state and no startup assignment. Values other than 2 remain
unknown rather than falsely appearing as Power, and do not trigger corrective
writes.

1121 is intentionally excluded. The official map calls it phase-C reactive power
setpoint, not Remote Control Status. Successful reading of zero proves neither
status semantics nor compatibility of writes.

## Encoding and validation

1109 uses the existing signed `rule: 2` decoder and two's-complement writer:
`wire = raw & 0xFFFF`; examples -1200 = 0xFB50, -1 = 0xFFFF, 0 = 0x0000,
+1200 = 0x04B0. There is no claim about charge/discharge direction. The requester's
signed-type confirmation underlies this choice; offline tests prove software
conversion, not device behavior. No watt conversion is introduced.

All writable entities explicitly specify FC03 reads and FC16 (0x10) writes. The
read-only watchdog status explicitly specifies FC03. The existing
writer reads the current register first and skips a write if unchanged. It is
therefore not suitable for watchdog heartbeat refresh without separate verification
and implementation. No heartbeat is part of this change.

The new opt-in `configurable.validate` validates a number's raw value before any
read or write. Decimal conversion rejects non-finite values, fractions of a raw
register unit and out-of-range values, avoiding float truncation. Existing number
entities retain their prior behavior. Home Assistant `number` bounds are contiguous,
so the watchdog is represented as a 10-18000 second number, a read-only status,
and a dedicated button that writes 65535. When the register reads 65535, the
number gets no out-of-range native value and the status is `Disabled`. This prevents
the UI from offering 18001-65534. 65535 is a sentinel, not a real timeout of
65535 seconds.

## Offline verification

The upstream checkout contains no unit-test harness or test dependencies; its CI
only runs hassfest and HACS checks. The new stdlib unittest tests require PyYAML:

```sh
python -m pip install PyYAML
python -m unittest discover -s tests -v
```

Tests execute actual production methods loaded through AST, with an entity shell
and an AsyncMock device. They never import/start a transport. This intentionally
avoids installing Home Assistant merely to check profile decoding and write calls.
It does not replace an HA integration/setup test, hassfest or HACS CI.

Nine tests pass on Python 3.12 / PyYAML 6.0.3: all 2401 representable power values,
all 1001 SOC values, FC03/FC16 call arguments, watchdog boundaries and sentinel button,
enum options, rejected inputs before I/O and entity construction without I/O. The
requester-supplied snapshot is also a regression fixture:
1100=0, 1101=65535, 1104=0, 1105=2, 1109=0, 1110=800 (80%).

## Risks and remaining target verification

Target: SUN-12K-SG05LP3-EU, MAIN 2107-1175-1809,
Communication 1001-C059-0000. The requester successfully read 1100-1121.
That establishes readable addresses only. No new live readings were made here.

- Confirm entity setup/poll scheduling in Home Assistant and that the snapshot
  appears correctly, especially watchdog-disabled and SOC=80%.
- These entities add 30-second polling to the shared P3 profile. Older firmware,
  other LP3/HP3 models and loggers may reject the block; there is no documented
  firmware capability threshold to gate it on. Test compatibility before broad
  upstream deployment.
- Obtain/archive the newer vendor revision and resolve the enum-range conflicts
  before adding Grid or Power+SOC options.
- In a separately authorized controlled hardware test, verify signed write/readback,
  power polarity (**unverified on target hardware**), rated-power reference, usable
  power limits, SOC strategy behavior and interactions with BMS/local limits.
- In that later phase, verify FC16 acceptance, persistence across restart, watchdog
  refresh/expiry/return to normal mode and the distinct remote mode semantics.
  Watchdog 65535 means no timeout protection; this change does not enable a timeout.
- The documented +/-120% range is a protocol limit, not a recommendation or a
  verified continuous operating limit for this inverter/battery.

Do not treat passing offline tests as authorization or evidence for live control.
