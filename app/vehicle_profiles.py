from dataclasses import dataclass


@dataclass(frozen=True)
class VehicleProfile:
    vehicle_id: str
    manufacturer: str
    model: str
    model_year: int

    max_rpm: float
    max_speed_kmh: float

    min_coolant_temp_c: float = -40
    max_coolant_temp_c: float = 150

    min_engine_load_percent: float = 0
    max_engine_load_percent: float = 100

    min_throttle_position_percent: float = 0
    max_throttle_position_percent: float = 100


VEHICLE_PROFILES = {
    "car_a": VehicleProfile(
        vehicle_id="car_a",
        manufacturer="Saipa",
        model="Pride",
        model_year=1390,
        max_rpm=7000,
        max_speed_kmh=180,
    ),
    "car_b": VehicleProfile(
        vehicle_id="car_b",
        manufacturer="Iran Khodro",
        model="206 TU5",
        model_year=1390,
        max_rpm=7500,
        max_speed_kmh=210,
    ),
}


def get_vehicle_profile(vehicle_id: str) -> VehicleProfile:
    try:
        return VEHICLE_PROFILES[vehicle_id]
    except KeyError:
        raise ValueError(
            f"Unsupported vehicle profile: {vehicle_id}"
        ) from None
