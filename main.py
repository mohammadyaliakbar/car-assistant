import json
from datetime import datetime, timezone
from pathlib import Path

from app.obd_client import OBDClient
from app.config import settings

from app.logger import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)

DATA_DIR = Path("data")
DATA_FILE = DATA_DIR / "vehicle_data.json"

def main():
    
    client = None

    try:
        client= OBDClient(settings.OBD_PORT)

        vehicle_data=client.read_vihecle_data()
        record = {
            "vehivle": "simulated_car",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": vehicle_data
        }

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with DATA_FILE.open("w",encoding="utf-8") as file:
            json.dump(
                record,
                file,
                ensure_ascii=False,
                indent=4
            )

        logger.info("Vehicle data saved successfully!")
        print(json.dumps(record,ensure_ascii=False,indent=4))

    except(ConnectionError, OSError, ValueError) as error:
        logger.error(f"Error: {error}")
    
    finally:
        if client is not None:
            client.close()

if __name__ == "__main__":
    main()