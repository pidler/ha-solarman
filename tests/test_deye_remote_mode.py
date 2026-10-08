"""Offline profile/production-method regression tests; no HA or transport imports.

Run: python -m unittest discover -s tests -v (requires PyYAML).
The repository has no HA test harness. AST loading executes the real production
classes/functions with a small entity shell and an AsyncMock device, rather than
copying the register conversion logic into tests. This is not an HA setup test.
"""
import ast
import copy
from decimal import Decimal, InvalidOperation
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/solarman"


class EntityShell:
    def __init__(self, coordinator, sensor):
        self.coordinator = coordinator
        self.registers = sensor["registers"]
        self.options = [entry["value"] for entry in sensor.get("lookup", [])]

    def set_state(self, *args):
        self.state = args

    def async_write_ha_state(self):
        pass


NAMESPACE = {
    "_LOGGER": logging.getLogger(__name__),
    "Decimal": Decimal, "InvalidOperation": InvalidOperation,
    "REQUEST_CODE": "code", "DIGITS": "digits",
    "EntityCategory": SimpleNamespace(CONFIG="config"),
    "FUNCTION_CODE": SimpleNamespace(READ_HOLDING_REGISTERS=3,
                                     WRITE_SINGLE_REGISTER=6,
                                     WRITE_MULTIPLE_REGISTERS=16),
    "SolarmanEntity": EntityShell,
    "NumberEntity": type("NumberEntity", (), {}),
    "SelectEntity": type("SelectEntity", (), {}),
    "ButtonEntity": type("ButtonEntity", (), {}),
}


def load_definitions(filename, names):
    tree = ast.parse((COMPONENT / filename).read_text(encoding="utf-8"))
    definitions = [node for node in tree.body if isinstance(
        node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    assert {node.name for node in definitions} == set(names)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[
        ast.alias(name="annotations")], level=0), *definitions], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(COMPONENT / filename), "exec"), NAMESPACE)


load_definitions("common.py", ["value_in_range", "get_code", "get_addr_value",
    "get_start_addr", "lookup_value", "from_bit_index", "get_number",
    "get_or_def", "ensure_list", "split_p16b"])
load_definitions("parser.py", ["ParameterParser"])
load_definitions("entity.py", ["SolarmanWritableEntity"])
load_definitions("number.py", ["SolarmanNumberEntity"])
load_definitions("select.py", ["SolarmanSelectEntity"])
load_definitions("button.py", ["SolarmanButtonEntity"])


class RemoteModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        profile = yaml.safe_load((COMPONENT / "inverter_definitions/deye_p3.yaml").read_text(encoding="utf-8"))
        group, = [g for g in profile["parameters"] if g["group"] == "Remote Control"]
        self.items = {item["name"]: dict(item, key=item["name"]) for item in group["items"]}
        self.items_by_register = {item["registers"][0]: item for item in self.items.values() if item["name"] != "Remote Watchdog Status" and item["platform"] != "button"}
        self.watchdog_status = self.items["Remote Watchdog Status"]
        self.device = SimpleNamespace(execute=AsyncMock(return_value=[0x8000]))
        self.coordinator = SimpleNamespace(device=self.device)
        parser_type = NAMESPACE["ParameterParser"]
        self.parser = parser_type.__new__(parser_type)
        self.parser._digits = 6
        self.parser._previous_result = {}

    def parse(self, address, raw):
        self.parser._result = {}
        item = self.items_by_register[address]
        method = self.parser.try_parse_signed if item["rule"] == 2 else self.parser.try_parse_unsigned
        method({(3, address): [raw]}, item)
        return self.parser._result.get(item["key"], (None,))[0]

    def entity(self, address):
        item = self.items_by_register[address]
        name = {"number": "SolarmanNumberEntity", "select": "SolarmanSelectEntity", "button": "SolarmanButtonEntity"}[item["platform"]]
        return NAMESPACE[name](self.coordinator, copy.deepcopy(item))

    def button(self, name):
        return NAMESPACE["SolarmanButtonEntity"](self.coordinator, copy.deepcopy(self.items[name]))

    async def assert_write(self, address, value, wire):
        self.device.execute.reset_mock()
        self.device.execute.side_effect = [[0x8000], 1]
        entity = self.entity(address)
        if self.items_by_register[address]["platform"] == "number":
            await entity.async_set_native_value(value)
        elif self.items_by_register[address]["platform"] == "select":
            await entity.async_select_option(value)
        else:
            await entity.async_press()
        calls = self.device.execute.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args, (3, address))
        self.assertEqual(calls[0].kwargs, {"count": 1})
        self.assertEqual(calls[1].args, (16, address))
        self.assertEqual(calls[1].kwargs, {"data": [wire]})

    def test_scope_and_entity_construction_has_no_writes(self):
        self.assertEqual(set(self.items_by_register), {1100, 1101, 1104, 1105, 1109, 1110})
        self.assertEqual(set(self.items), {"Remote Mode", "Remote Watchdog", "Remote Watchdog Status", "Disable Remote Watchdog", "Remote Power Control Mode", "Remote Battery-side Control Strategy", "Remote Constant Power Setpoint", "Remote Constant SOC Setpoint"})
        for item in self.items.values():
            if item["name"] == "Remote Watchdog Status":
                self.assertEqual(item["code"], 3)
                continue
            self.assertEqual(item["code"], {"read": 3, "write": 16})
            self.assertNotIn("writeback", item)
            self.entity(item["registers"][0]) if item["platform"] != "button" else self.button(item["name"])
        self.device.execute.assert_not_called()
        self.assertEqual(self.items_by_register[1105]["lookup"], [{"key": 2, "value": "Power"}])

    def test_target_read_only_snapshot(self):
        expected = {1100: (0, "Disabled"),
                    1104: (0, "AC-side control"), 1105: (2, "Power"),
                    1109: (0, 0), 1110: (800, 80)}
        for address, (raw, value) in expected.items():
            with self.subTest(address=address):
                self.assertEqual(self.parse(address, raw), value)

    def test_disabled_watchdog_uses_read_only_status_not_out_of_range_number(self):
        self.parser._result = {"Remote Watchdog": (None, None)}
        self.assertIsNone(self.parse(1101, 65535))
        self.assertNotIn("Remote Watchdog", self.parser._result)
        self.parser._result = {"Remote Watchdog": (None, None)}
        self.parser.try_parse_unsigned({(3, 1101): [65535]}, self.items_by_register[1101])
        self.assertEqual(self.parser._result["Remote Watchdog"], (None, None))
        self.parser._result = {}
        self.parser.try_parse_unsigned({(3, 1101): [65535]}, self.watchdog_status)
        self.assertEqual(self.parser._result["Remote Watchdog Status"], ("Disabled", 65535))

    async def test_all_signed_power_values_round_trip_and_fc16(self):
        # Includes -120 -> 0xFB50, -0.1 -> 0xFFFF, 0, +120 -> 0x04B0.
        for raw in range(-1200, 1201):
            value = float(Decimal(raw) / 10)
            with self.subTest(raw=raw):
                self.assertEqual(self.parse(1109, raw & 0xFFFF), value)
                await self.assert_write(1109, value, raw & 0xFFFF)

    async def test_all_soc_values_round_trip_and_fc16(self):
        # Decimal conversion prevents int(0.3 / 0.1) silently becoming 2.
        for raw in range(1001):
            value = float(Decimal(raw) / 10)
            with self.subTest(raw=raw):
                self.assertEqual(self.parse(1110, raw), value)
                await self.assert_write(1110, value, raw)

    async def test_watchdog_and_select_writes_use_fc16(self):
        for seconds in (10, 60, 18000):
            await self.assert_write(1101, seconds, seconds)
        self.device.execute.reset_mock()
        self.device.execute.side_effect = [[0], 1]
        await self.button("Disable Remote Watchdog").async_press()
        self.assertEqual(self.device.execute.await_args_list[0].args, (3, 1101))
        self.assertEqual(self.device.execute.await_args_list[0].kwargs, {"count": 1})
        self.assertEqual(self.device.execute.await_args_list[1].args, (16, 1101))
        self.assertEqual(self.device.execute.await_args_list[1].kwargs, {"data": [65535]})
        for address in (1100, 1104, 1105):
            for option in self.items_by_register[address]["lookup"]:
                await self.assert_write(address, option["value"], option["key"])

    async def test_invalid_numbers_rejected_before_any_io(self):
        cases = {1101: [-1, 0, 9, 10.5, 18001, 65534, 65536],
                 1109: [-120.1, 120.1, 0.01], 1110: [-0.1, 100.1, 80.01]}
        for address, values in cases.items():
            for value in [*values, float("nan"), float("inf"), -float("inf")]:
                with self.subTest(address=address, value=value):
                    self.device.execute.reset_mock()
                    with self.assertRaises(ValueError):
                        await self.entity(address).async_set_native_value(value)
                    self.device.execute.assert_not_called()

    async def test_invalid_selects_rejected_before_any_io(self):
        for address in (1100, 1104, 1105):
            with self.assertRaises(ValueError):
                await self.entity(address).async_select_option("Unsupported")
        self.device.execute.assert_not_called()

    def test_invalid_reads_are_not_reported_as_a_valid_mode(self):
        cases = {1100: [4, 65535], 1101: [0, 9, 18001, 65534],
                 1104: [2, 65535], 1105: [0, 1, 3, 4, 5, 65535],
                 1109: [1201, (-1201) & 0xFFFF, 0x8000], 1110: [1001, 65535]}
        for address, values in cases.items():
            for value in values:
                with self.subTest(address=address, value=value):
                    self.assertIsNone(self.parse(address, value))

if __name__ == "__main__":
    unittest.main()
