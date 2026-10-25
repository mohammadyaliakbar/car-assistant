import obd


class OBDClient:
    def __init__(self, port: str):
        self.connection = obd.OBD(
            portstr=port,
            baudrate=38400,
            fast=False,
            timeout=5,
        )

        if not self.connection.is_connected():
            self.connection.close()
            raise ConnectionError("Could not connect to the vehicle.")

    @staticmethod
    def _normalize_response(response):
        if response.is_null():
            return None

        value = response.value

        # مقادیر دارای واحد، مانند RPM و SPEED
        if hasattr(value, "magnitude"):
            return float(value.magnitude)

        # فهرست کدهای خطا، مانند GET_DTC
        if isinstance(value, list):
            return [
                str(item[0]) if isinstance(item, tuple) else str(item)
                for item in value
            ]

        # مقادیر عددی ساده
        if isinstance(value, (int, float)):
            return float(value)

        # سایر پاسخ‌ها، مانند STATUS
        return str(value)

    def read_vehicle_data(self) -> dict:
        commands = {
            "rpm": obd.commands.RPM,
            "speed": obd.commands.SPEED,
            "coolant_temperature_c": obd.commands.COOLANT_TEMP,
            "engine_load_percent": obd.commands.ENGINE_LOAD,
            "throttle_position_percent": obd.commands.THROTTLE_POS,
            "intake_temperature_c": obd.commands.INTAKE_TEMP,
            "intake_pressure_kpa": obd.commands.INTAKE_PRESSURE,
            "trouble_codes": obd.commands.GET_DTC,
            "diagnostic_status": obd.commands.STATUS,
            "control_module_voltage_v": obd.commands.CONTROL_MODULE_VOLTAGE,
        }

        data = {}

        for name, command in commands.items():
            if not self.connection.supports(command):
                data[name] = None
                continue

            try:
                response = self.connection.query(command)
                data[name] = self._normalize_response(response)
            except (ValueError, TypeError, AttributeError):
                data[name] = None

        return data

    def close(self):
        self.connection.close()
