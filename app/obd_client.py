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

    def read_vehicle_data(self) -> dict:
        commands = {
            "rpm": obd.commands.RPM,
            "speed": obd.commands.SPEED,
            "coolant_temperature_c": obd.commands.COOLANT_TEMP,
        }

        data = {}

        for name, command in commands.items():
            response = self.connection.query(command)

            if response.is_null():
                data[name] = None
            else:
                data[name] = float(response.value.magnitude)

        return data

    def close(self):
        self.connection.close()

