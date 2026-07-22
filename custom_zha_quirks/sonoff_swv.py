"""SONOFF SWV - Zigbee smart water valve.

Replaces the stock `zhaquirks.sonoff.swv` quirk and adds the irrigation features the
Zigbee2MQTT converter exposes but ZHA does not: the current-session duration and
volume, the session timestamps, the daily volume, the running-program flag, and the
two cyclic irrigation programs (timed and quantitative).

Install: copy this file into ZHA's custom quirks directory - by convention
`/config/custom_zha_quirks/` - point ZHA at it (Settings -> Devices & services ->
Zigbee Home Automation -> Configure -> Custom quirks path) and restart Home
Assistant. Custom quirks are registered after the built-in ones and are matched
first, so this file replaces the stock quirk wholesale; its three entities are
re-declared below with their original unique-id suffixes and translation keys so
existing entity IDs and names survive the swap.

The cyclic irrigation programs are the interesting part. The device packs a whole
program into a single attribute (0x5008 timed, 0x5009 quantitative) as a ten-byte
blob, which is useless as a Home Assistant entity. This quirk unpacks each blob into
per-field virtual attributes (0xFF00-0xFF07) exposed as normal numbers and sensors.

Writing a program does not merely configure it - it *starts* the watering, with no
switch-on needed (measured on firmware 1.0.4). So the numbers do not write through:
they stage their values in the cluster's cache, and a "Start irrigation" button
(0xFF08/0xFF09) packs them into the one write that runs the program. Setting the
fields straight to the device would start a watering per field, each with a
half-built program.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import zigpy.types as t
from zigpy.typing import UNDEFINED
from zigpy.zcl import foundation
from zigpy.zcl.foundation import BaseAttributeDefs, DataTypeId, ZCLAttributeDef

from zhaquirks.builder import (
    BinarySensorDeviceClass,
    EntityType,
    NumberDeviceClass,
    QuirkBuilder,
    ReportingConfig,
    SensorDeviceClass,
    SensorStateClass,
    UnitOfTime,
    UnitOfVolume,
)
from zhaquirks.clusters import CustomCluster

EWELINK_CLUSTER_ID = 0xFC11


def as_timestamp(value: int) -> datetime | None:
    """Convert an irrigation session timestamp into an aware datetime.

    The epoch is 1970 as Sonoff's converter says, but the seconds are *local* wall
    clock, not UTC - the valve takes its clock from the ZCL Time cluster's localTime
    and stores that verbatim. Measured against a real run: a session that started at
    11:00:21 UTC was reported as 1784761220, which reads as 21:00:20 UTC, exactly the
    +10:00 offset of the hub serving the time.

    So the value is turned back into the naive wall clock it really is, then stamped
    with this machine's zone - the same zone the device was given.
    """
    if not value:
        return None

    naive = datetime.fromtimestamp(value, tz=UTC).replace(tzinfo=None)
    return naive.astimezone()


class ValveState(t.enum8):
    """Water valve state."""

    Normal = 0
    Water_Shortage = 1
    Water_Leakage = 2
    Water_Shortage_And_Leakage = 3


class CyclicIrrigation(t.LVBytes):
    """The payload behind the cyclic irrigation attributes.

    The device carries it in a ZCL character-string attribute (type 0x42), so zigpy
    deserializes it as a `CharacterString` whose *text* is unusable - the payload is
    binary and decoding truncates it at the first NUL. The undecoded bytes survive on
    the string's `raw` attribute, which is what we pick up here.

    Program layout: cycles-done:u8, cycles-total:u8, amount:u32 big-endian,
    interval:u32 big-endian. "Amount" is seconds for the timed program and litres for
    the quantitative one.

    Measured behaviour on firmware 1.0.4:

    * the program is carried as the bare 10 bytes, the same in both directions;
    * **every field must be non-zero.** A program with a zero duration, interval or
      cycle count is refused with INVALID_VALUE - even the interval, which does
      nothing on a single-cycle run. The numbers below are floored at 1 for that
      reason;
    * once a program finishes the device reports a short 2-byte form, just done and
      total, so the last known duration and interval have to be kept locally.
    """

    PROGRAM_LENGTH = 10

    def __new__(cls, value: Any = b"") -> CyclicIrrigation:
        """Accept raw bytes, another instance, or a zigpy `CharacterString`."""
        raw = getattr(value, "raw", None)
        if isinstance(raw, (bytes, bytearray)):
            value = raw

        return super().__new__(cls, value)

    @classmethod
    def from_fields(cls, fields: list[int]) -> CyclicIrrigation:
        """Pack the four program fields into the form the device accepts.

        The three settable fields are floored at 1: the valve refuses a program
        containing a zero with INVALID_VALUE, and a rejected write is a watering that
        silently never happens.
        """
        done, total, amount, interval = (f or 0 for f in fields)
        return cls(
            bytes([done & 0xFF, max(total, 1) & 0xFF])
            + t.uint32_t_be(max(amount, 1)).serialize()
            + t.uint32_t_be(max(interval, 1)).serialize()
        )

    @property
    def fields(self) -> list[int | None] | None:
        """Unpack into [cycles done, cycles total, amount, interval], or None.

        The trailing two are None for the short form the device reports when a
        program completes, so callers can leave the last known values in place
        rather than blanking them.
        """
        if len(self) == 2:
            return [self[0], self[1], None, None]

        if len(self) != self.PROGRAM_LENGTH:
            return None

        return [
            self[0],
            self[1],
            int.from_bytes(self[2:6], "big"),
            int.from_bytes(self[6:10], "big"),
        ]


class SonoffWaterValveCluster(CustomCluster):
    """eWeLink manufacturer cluster as the SWV uses it."""

    cluster_id = EWELINK_CLUSTER_ID

    class AttributeDefs(BaseAttributeDefs):
        """Attribute definitions."""

        real_time_irrigation_duration = ZCLAttributeDef(
            id=0x5006, type=t.uint32_t, manufacturer_code=None
        )
        real_time_irrigation_volume = ZCLAttributeDef(
            id=0x5007, type=t.uint32_t, manufacturer_code=None
        )
        cyclic_timed_irrigation = ZCLAttributeDef(
            id=0x5008,
            type=CyclicIrrigation,
            zcl_type=DataTypeId.string,
            manufacturer_code=None,
        )
        cyclic_quantitative_irrigation = ZCLAttributeDef(
            id=0x5009,
            type=CyclicIrrigation,
            zcl_type=DataTypeId.string,
            manufacturer_code=None,
        )
        water_valve_state = ZCLAttributeDef(
            id=0x500C, type=ValveState, manufacturer_code=None
        )
        irrigation_start_time = ZCLAttributeDef(
            id=0x500D, type=t.uint32_t, manufacturer_code=None
        )
        irrigation_end_time = ZCLAttributeDef(
            id=0x500E, type=t.uint32_t, manufacturer_code=None
        )
        daily_irrigation_volume = ZCLAttributeDef(
            id=0x500F, type=t.uint32_t, manufacturer_code=None
        )
        valve_work_state = ZCLAttributeDef(
            id=0x5010, type=t.Bool, manufacturer_code=None
        )
        auto_close_water_shortage = ZCLAttributeDef(
            id=0x5011, type=t.uint16_t, manufacturer_code=None
        )

        # Virtual attributes: the individual fields of the two packed program blobs
        # above. They never travel over the air - reads and writes are translated
        # into reads and writes of their parent attribute.
        timed_cycles_done = ZCLAttributeDef(
            id=0xFF00, type=t.uint8_t, manufacturer_code=None
        )
        timed_cycles_total = ZCLAttributeDef(
            id=0xFF01, type=t.uint8_t, manufacturer_code=None
        )
        timed_duration = ZCLAttributeDef(
            id=0xFF02, type=t.uint32_t, manufacturer_code=None
        )
        timed_interval = ZCLAttributeDef(
            id=0xFF03, type=t.uint32_t, manufacturer_code=None
        )
        quantitative_cycles_done = ZCLAttributeDef(
            id=0xFF04, type=t.uint8_t, manufacturer_code=None
        )
        quantitative_cycles_total = ZCLAttributeDef(
            id=0xFF05, type=t.uint8_t, manufacturer_code=None
        )
        quantitative_volume = ZCLAttributeDef(
            id=0xFF06, type=t.uint32_t, manufacturer_code=None
        )
        quantitative_interval = ZCLAttributeDef(
            id=0xFF07, type=t.uint32_t, manufacturer_code=None
        )

        # Commit points. Writing either one packs the staged fields above into its
        # program and sends that, which is what actually starts the watering.
        start_timed_irrigation = ZCLAttributeDef(
            id=0xFF08, type=t.uint8_t, manufacturer_code=None
        )
        start_quantitative_irrigation = ZCLAttributeDef(
            id=0xFF09, type=t.uint8_t, manufacturer_code=None
        )

    # virtual attribute id -> (packed attribute id, field index)
    VIRTUAL_ATTRIBUTES: dict[int, tuple[int, int]] = {
        0xFF00: (0x5008, 0),
        0xFF01: (0x5008, 1),
        0xFF02: (0x5008, 2),
        0xFF03: (0x5008, 3),
        0xFF04: (0x5009, 0),
        0xFF05: (0x5009, 1),
        0xFF06: (0x5009, 2),
        0xFF07: (0x5009, 3),
    }

    # start attribute id -> the packed program it commits
    START_ATTRIBUTES: dict[int, int] = {
        0xFF08: 0x5008,
        0xFF09: 0x5009,
    }

    def _local_id(self, attribute: Any, manufacturer: Any = UNDEFINED) -> int | None:
        """Return the id of `attribute` if this quirk handles it locally."""
        try:
            attr_def = self.find_attribute(attribute, manufacturer_code=manufacturer)
        except KeyError:
            return None

        if attr_def.id in self.VIRTUAL_ATTRIBUTES or attr_def.id in self.START_ATTRIBUTES:
            return attr_def.id

        return None

    def _program_fields(self, packed_id: int) -> list[int]:
        """Decode the program the device last reported for a packed attribute."""
        packed = super().get(packed_id)
        fields = CyclicIrrigation(packed).fields if packed is not None else None
        if fields is None:
            return [0, 0, 0, 0]
        return [0 if f is None else f for f in fields]

    def _staged_fields(self, packed_id: int) -> list[int]:
        """Collect the staged numbers for a program, ready to commit.

        The cycles-done counter is the device's to keep, so it is always sent as
        zero - which is what Sonoff's own converter does.
        """
        fields = [0, 0, 0, 0]
        for virtual_id, (parent_id, index) in self.VIRTUAL_ATTRIBUTES.items():
            if parent_id == packed_id and index:
                fields[index] = int(self.get(virtual_id) or 0)

        return fields

    def _update_attribute(self, attrid: int, value: Any) -> None:
        """Fan a packed program out over its virtual attributes."""
        super()._update_attribute(attrid, value)

        if attrid not in (0x5008, 0x5009):
            return

        fields = CyclicIrrigation(value).fields
        if fields is None:
            return

        for virtual_id, (packed_id, index) in self.VIRTUAL_ATTRIBUTES.items():
            # A None field is absent from the short form, not zero; leaving the
            # cached value alone beats blanking a program we still know.
            if packed_id == attrid and fields[index] is not None:
                super()._update_attribute(virtual_id, fields[index])

    def get(self, key: int | str, default: Any | None = None) -> Any:
        """Get a cached attribute, deriving this quirk's own when they are missing.

        ZHA refuses to create a number entity whose attribute has no cached value, so
        an as-yet-unseen program blob would cost us all six program numbers on a first
        run. Falling back to the packed attribute (and to zero when even that is
        unknown) keeps the entities, and the real values land as soon as the device
        answers.
        """
        local_id = self._local_id(key)
        if local_id is None:
            return super().get(key, default)

        cached = super().get(key)
        if cached is not None:
            return cached

        if local_id in self.START_ATTRIBUTES:
            return 0

        packed_id, index = self.VIRTUAL_ATTRIBUTES[local_id]
        return self._program_fields(packed_id)[index]

    async def read_attributes(
        self,
        attributes: list[Any],
        allow_cache: bool = False,
        only_cache: bool = False,
        manufacturer: Any = UNDEFINED,
        **kwargs: Any,
    ) -> tuple[dict, dict]:
        """Serve this quirk's own attributes locally.

        Staged numbers are answered from the packed program they came from; the start
        attributes never existed on the device at all. Letting either reach the device
        would draw an UNSUPPORTED_ATTRIBUTE and get them marked unsupported for good.
        """
        local: dict[Any, int] = {}
        passthrough: list[Any] = []

        for attribute in attributes:
            local_id = self._local_id(attribute, manufacturer)
            if local_id is None:
                passthrough.append(attribute)
            else:
                local[attribute] = local_id

        if not local:
            return await super().read_attributes(
                attributes,
                allow_cache=allow_cache,
                only_cache=only_cache,
                manufacturer=manufacturer,
                **kwargs,
            )

        # Parents the caller did not ask for are read on its behalf and then stripped
        # from the result, so it only ever sees what it requested.
        requested_ids = {
            attr_def.id
            for attr_def in (
                self._find_attribute_or_none(attribute, manufacturer)
                for attribute in passthrough
            )
            if attr_def is not None
        }
        borrowed = sorted(
            {
                self.VIRTUAL_ATTRIBUTES[local_id][0]
                for local_id in local.values()
                if local_id in self.VIRTUAL_ATTRIBUTES
            }
            - requested_ids
        )

        success: dict = {}
        failure: dict = {}

        if passthrough or borrowed:
            success, failure = await super().read_attributes(
                passthrough + borrowed,
                allow_cache=allow_cache,
                only_cache=only_cache,
                manufacturer=manufacturer,
                **kwargs,
            )

        for packed_id in borrowed:
            success.pop(packed_id, None)
            failure.pop(packed_id, None)

        for attribute, local_id in local.items():
            success[attribute] = self.get(local_id)

        return success, failure

    async def write_attributes(
        self,
        attributes: dict[Any, Any],
        manufacturer: Any = UNDEFINED,
        **kwargs: Any,
    ) -> list[list[foundation.WriteAttributesStatusRecord]]:
        """Stage the program numbers locally; only a start attribute reaches the device.

        Writing a program to this valve *starts* it - measured on firmware 1.0.4, the
        water runs the moment the attribute lands, with no switch-on needed. So the
        six numbers cannot write through: setting them one at a time would kick off
        six waterings, the first five with a half-built program. They are held in the
        cluster's cache until a start attribute packs them into one write.
        """
        staged: dict[int, int] = {}
        starts: list[int] = []
        passthrough: dict[Any, Any] = {}

        for attribute, value in attributes.items():
            local_id = self._local_id(attribute, manufacturer)
            if local_id is None:
                passthrough[attribute] = value
            elif local_id in self.START_ATTRIBUTES:
                packed_id = self.START_ATTRIBUTES[local_id]
                if packed_id not in starts:
                    starts.append(packed_id)
            else:
                staged[local_id] = int(value)

        if not staged and not starts:
            return await super().write_attributes(
                attributes, manufacturer=manufacturer, **kwargs
            )

        records: list[foundation.WriteAttributesStatusRecord] = []

        if passthrough:
            result = await super().write_attributes(
                passthrough, manufacturer=manufacturer, **kwargs
            )
            records.extend(result[0])

        for virtual_id, value in staged.items():
            # Cache-only, but still report success: HA raises on any non-SUCCESS
            # record, and the number entity reads its new state back out of here.
            self._update_attribute(virtual_id, value)
            records.append(
                foundation.WriteAttributesStatusRecord(
                    status=foundation.Status.SUCCESS, attrid=virtual_id
                )
            )

        for packed_id in starts:
            packed = CyclicIrrigation.from_fields(self._staged_fields(packed_id))
            result = await super().write_attributes(
                {packed_id: packed}, manufacturer=manufacturer, **kwargs
            )
            records.extend(result[0])

            if all(
                record.status == foundation.Status.SUCCESS for record in result[0]
            ):
                # A successful write only sets the cache directly, bypassing
                # `_update_attribute` - so fan the new program out by hand, or the
                # number entities keep showing the old values until the next report.
                self._update_attribute(packed_id, packed)

        return [records]

    def _find_attribute_or_none(
        self, attribute: Any, manufacturer: Any
    ) -> foundation.ZCLAttributeDef | None:
        """Look up an attribute definition, tolerating unknown attributes."""
        try:
            return self.find_attribute(attribute, manufacturer_code=manufacturer)
        except KeyError:
            return None


(
    QuirkBuilder("SONOFF", "SWV")
    .replaces(SonoffWaterValveCluster)
    # --- entities carried over from the stock quirk, unchanged -------------------
    .binary_sensor(
        SonoffWaterValveCluster.AttributeDefs.water_valve_state.name,
        SonoffWaterValveCluster.cluster_id,
        device_class=BinarySensorDeviceClass.MOISTURE,
        attribute_converter=lambda x: x & ValveState.Water_Leakage,
        unique_id_suffix="water_leak_status",
        reporting_config=ReportingConfig(
            min_interval=30, max_interval=900, reportable_change=1
        ),
        translation_key="water_leak",
        fallback_name="Water leak",
    )
    .binary_sensor(
        SonoffWaterValveCluster.AttributeDefs.water_valve_state.name,
        SonoffWaterValveCluster.cluster_id,
        device_class=BinarySensorDeviceClass.PROBLEM,
        attribute_converter=lambda x: x & ValveState.Water_Shortage,
        unique_id_suffix="water_supply_status",
        translation_key="water_supply",
        fallback_name="Water supply",
    )
    .switch(
        SonoffWaterValveCluster.AttributeDefs.auto_close_water_shortage.name,
        SonoffWaterValveCluster.cluster_id,
        off_value=0,
        on_value=30,
        translation_key="water_shortage_auto_close",
        fallback_name="Water shortage auto-close",
    )
    # --- current / last irrigation session ---------------------------------------
    .binary_sensor(
        SonoffWaterValveCluster.AttributeDefs.valve_work_state.name,
        SonoffWaterValveCluster.cluster_id,
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_type=EntityType.STANDARD,
        attribute_initialized_from_cache=False,
        unique_id_suffix="valve_work_state",
        translation_key="swv_program_running",
        fallback_name="Irrigation program running",
    )
    .sensor(
        SonoffWaterValveCluster.AttributeDefs.real_time_irrigation_duration.name,
        SonoffWaterValveCluster.cluster_id,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        unit=UnitOfTime.SECONDS,
        suggested_display_precision=0,
        attribute_initialized_from_cache=False,
        unique_id_suffix="irrigation_duration",
        translation_key="swv_irrigation_duration",
        fallback_name="Irrigation duration",
    )
    .sensor(
        SonoffWaterValveCluster.AttributeDefs.real_time_irrigation_volume.name,
        SonoffWaterValveCluster.cluster_id,
        state_class=SensorStateClass.MEASUREMENT,
        unit=UnitOfVolume.LITERS,
        suggested_display_precision=0,
        attribute_initialized_from_cache=False,
        device_class=SensorDeviceClass.VOLUME,
        unique_id_suffix="irrigation_volume",
        translation_key="swv_irrigation_volume",
        fallback_name="Irrigation volume",
    )
    .sensor(
        SonoffWaterValveCluster.AttributeDefs.daily_irrigation_volume.name,
        SonoffWaterValveCluster.cluster_id,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        unit=UnitOfVolume.LITERS,
        suggested_display_precision=0,
        attribute_initialized_from_cache=False,
        unique_id_suffix="daily_irrigation_volume",
        translation_key="swv_daily_irrigation_volume",
        fallback_name="Daily irrigation volume",
    )
    .sensor(
        SonoffWaterValveCluster.AttributeDefs.irrigation_start_time.name,
        SonoffWaterValveCluster.cluster_id,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_type=EntityType.DIAGNOSTIC,
        attribute_converter=as_timestamp,
        attribute_initialized_from_cache=False,
        unique_id_suffix="irrigation_start_time",
        translation_key="swv_irrigation_start_time",
        fallback_name="Irrigation start time",
    )
    .sensor(
        SonoffWaterValveCluster.AttributeDefs.irrigation_end_time.name,
        SonoffWaterValveCluster.cluster_id,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_type=EntityType.DIAGNOSTIC,
        attribute_converter=as_timestamp,
        attribute_initialized_from_cache=False,
        unique_id_suffix="irrigation_end_time",
        translation_key="swv_irrigation_end_time",
        fallback_name="Irrigation end time",
    )
    # --- cyclic timed irrigation program -----------------------------------------
    .number(
        SonoffWaterValveCluster.AttributeDefs.timed_cycles_total.name,
        SonoffWaterValveCluster.cluster_id,
        min_value=1,
        max_value=100,
        step=1,
        mode="box",
        attribute_initialized_from_cache=False,
        unique_id_suffix="timed_cycles_total",
        translation_key="swv_timed_cycles_total",
        fallback_name="Timed irrigation cycles",
    )
    .number(
        SonoffWaterValveCluster.AttributeDefs.timed_duration.name,
        SonoffWaterValveCluster.cluster_id,
        min_value=1,
        max_value=86400,
        step=1,
        unit=UnitOfTime.SECONDS,
        mode="box",
        attribute_initialized_from_cache=False,
        device_class=NumberDeviceClass.DURATION,
        unique_id_suffix="timed_duration",
        translation_key="swv_timed_duration",
        fallback_name="Timed irrigation duration",
    )
    .number(
        SonoffWaterValveCluster.AttributeDefs.timed_interval.name,
        SonoffWaterValveCluster.cluster_id,
        min_value=1,
        max_value=86400,
        step=1,
        unit=UnitOfTime.SECONDS,
        mode="box",
        attribute_initialized_from_cache=False,
        device_class=NumberDeviceClass.DURATION,
        unique_id_suffix="timed_interval",
        translation_key="swv_timed_interval",
        fallback_name="Timed irrigation interval",
    )
    .sensor(
        SonoffWaterValveCluster.AttributeDefs.timed_cycles_done.name,
        SonoffWaterValveCluster.cluster_id,
        entity_type=EntityType.DIAGNOSTIC,
        suggested_display_precision=0,
        attribute_initialized_from_cache=False,
        unique_id_suffix="timed_cycles_done",
        translation_key="swv_timed_cycles_done",
        fallback_name="Timed irrigation cycles completed",
    )
    # --- cyclic quantitative irrigation program ----------------------------------
    .number(
        SonoffWaterValveCluster.AttributeDefs.quantitative_cycles_total.name,
        SonoffWaterValveCluster.cluster_id,
        min_value=1,
        max_value=100,
        step=1,
        mode="box",
        attribute_initialized_from_cache=False,
        unique_id_suffix="quantitative_cycles_total",
        translation_key="swv_quantitative_cycles_total",
        fallback_name="Quantitative irrigation cycles",
    )
    .number(
        SonoffWaterValveCluster.AttributeDefs.quantitative_volume.name,
        SonoffWaterValveCluster.cluster_id,
        min_value=1,
        max_value=6500,
        step=1,
        unit=UnitOfVolume.LITERS,
        mode="box",
        attribute_initialized_from_cache=False,
        device_class=NumberDeviceClass.VOLUME,
        unique_id_suffix="quantitative_volume",
        translation_key="swv_quantitative_volume",
        fallback_name="Quantitative irrigation volume",
    )
    .number(
        SonoffWaterValveCluster.AttributeDefs.quantitative_interval.name,
        SonoffWaterValveCluster.cluster_id,
        min_value=1,
        max_value=86400,
        step=1,
        unit=UnitOfTime.SECONDS,
        mode="box",
        attribute_initialized_from_cache=False,
        device_class=NumberDeviceClass.DURATION,
        unique_id_suffix="quantitative_interval",
        translation_key="swv_quantitative_interval",
        fallback_name="Quantitative irrigation interval",
    )
    .sensor(
        SonoffWaterValveCluster.AttributeDefs.quantitative_cycles_done.name,
        SonoffWaterValveCluster.cluster_id,
        entity_type=EntityType.DIAGNOSTIC,
        suggested_display_precision=0,
        attribute_initialized_from_cache=False,
        unique_id_suffix="quantitative_cycles_done",
        translation_key="swv_quantitative_cycles_done",
        fallback_name="Quantitative irrigation cycles completed",
    )
    # --- commit the staged programs ----------------------------------------------
    # The written value is ignored; it is the write itself that starts the valve.
    .write_attr_button(
        SonoffWaterValveCluster.AttributeDefs.start_timed_irrigation.name,
        1,
        SonoffWaterValveCluster.cluster_id,
        entity_type=EntityType.STANDARD,
        unique_id_suffix="start_timed_irrigation",
        translation_key="swv_start_timed_irrigation",
        fallback_name="Start timed irrigation",
    )
    .write_attr_button(
        SonoffWaterValveCluster.AttributeDefs.start_quantitative_irrigation.name,
        1,
        SonoffWaterValveCluster.cluster_id,
        entity_type=EntityType.STANDARD,
        unique_id_suffix="start_quantitative_irrigation",
        translation_key="swv_start_quantitative_irrigation",
        fallback_name="Start quantitative irrigation",
    )
    .add_to_registry()
)
