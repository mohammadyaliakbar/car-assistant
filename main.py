import json
from pathlib import Path

from app.config import settings
from app.obd_client import OBDClient
from app.data_models import VehicleData, VehicleRecord
from app.logger import get_logger, setup_logging
from app.vehicle_profiles import get_vehicle_profile
from app.data_validation import DataValidator

setup_logging()
logger = get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_FILE = DATA_DIR / "vehicle_data.json"
  
def main():
    
    client = None
    try:
        #car profile
        profile = get_vehicle_profile(settings.VEHICLE_ID)

        #connect to obd and retrieve raw data
        client= OBDClient(settings.OBD_PORT)
        raw_data= client.read_vehicle_data()

        #validate data with profile car 
        validator = DataValidator(profile)
        report = validator.validate(raw_data)

        if report.errors:
            for error in report.errors:
                logger.error("Validation error: %s", error)

            return

        for warning in report.warnings:
            logger.warning("Validation warning: %s", warning)

        vehicle_data = report.data

        if vehicle_data is None:
            logger.error("No valid vehicle data available.")
            return

        # Creat record
        record= VehicleRecord(
            vehicle_id=profile.vehicle_id,
            source= "OBD",
            data= vehicle_data
        )

        #save data
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        with DATA_FILE.open("w",encoding="utf-8") as file:
            json.dump(
                record.model_dump(mode="json"),
                file,
                ensure_ascii=False,
                indent=4
            )

        logger.info(
            "Vehicle data saved successfully for %s %s.",
            profile.manufacturer,
            profile.model,
        )
        print(
            json.dumps(
                record.model_dump(mode="json"),
                ensure_ascii=False,
                indent=4
            )
        )

    #Detector


    except (ConnectionError, OSError, ValueError) as error:
        logger.exception("Failed to read or save vehicle data: %s", error)
    
    except Exception as error:
        logger.exception("Unexpected error while processing vehicle data.")

    finally:
        if client is not None:
            client.close()
if __name__ == "__main__":
    main()