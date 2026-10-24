import json
from pathlib import Path

from app.config import settings
from app.obd_client import OBDClient
from app.data_models import vehicleData, vehicleRecord
from app.logger import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_FILE = DATA_DIR / "vehicle_data.json"

def main():
    
    
    try:

        client= OBDClient(settings.OBD_PORT)
        raw_data= client.read_vehicle_data()
        vehicle_data= vehicleData.model_validate(raw_data)

        record= vehicleRecord(
            vehicle_id="test vehicle",
            source= "OBD",
            data= vehicle_data
        )
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with DATA_FILE.open("w",encoding="utf-8") as file:
            json.dump(
                record.model_dump(mode="json"),
                file,
                ensure_ascii=False,
                indent=4
            )

        logger.info("Vehicle data saved successfully!")
        print(
            json.dumps(
                record.model_dump(mode="json"),
                ensure_ascii=False,
                indent=4
            )
        )

    except (ConnectionError, OSError, ValueError) as error:
        logger.exception("Failed to read or save vehicle data: %s", error)
    
    except Exception as error:
        logger.exception("Unexpected error while processing vehicle data.")

    finally:
        if client is not None:
            client.close()
if __name__ == "__main__":
    main()