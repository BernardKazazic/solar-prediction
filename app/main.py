from fastapi import FastAPI, HTTPException, Query, UploadFile, File
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import List
from app.prediction.data_preparation_service import DataPreparationService
from app.prediction.prediction_repository import PredictionRepository
from app.prediction.prediction_service import PredictionService
from app.prediction.prediction_models import ForecastResponse
from app.prediction.scheduling import PredictionScheduler
from app.prediction.state.model_manager_connector import ModelManagerConnector
from app.prediction.state.state_manager import StateManager
from app.prediction.weather_forecast.open_meteo_connector import OpenMeteoConnector
from app.config.database import db_manager
from app.prediction.weather_forecast.weather_forecast_repository import (
    WeatherForecastRepository,
)
from app.prediction.weather_forecast.weather_forecast_service import (
    WeatherForecastService,
)
from app.prediction.power_readings.power_readings_repository import (
    PowerReadingsRepository,
)
from app.prediction.power_readings.power_readings_service import PowerReadingsService
from app.prediction.power_readings.power_readings_models import CSVUploadResponse

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

OPEN_METEO_BASE_URL = "https://api.open-meteo.com/v1/forecast"
MODEL_MANAGER_BASE_URL = "http://localhost:8000"

model_manager_connector = ModelManagerConnector(base_url=MODEL_MANAGER_BASE_URL)
state_manager = StateManager(model_manager_connector=model_manager_connector)

open_meteo_connector = OpenMeteoConnector(base_url=OPEN_METEO_BASE_URL)
weather_forecast_repository = WeatherForecastRepository()
weather_forecast_service = WeatherForecastService(
    open_meteo_connector=open_meteo_connector,
    weather_forecast_repository=weather_forecast_repository,
)

data_preparation_service = DataPreparationService()
prediction_repository = PredictionRepository()
prediction_service = PredictionService(
    state_manager=state_manager,
    weather_forecast_service=weather_forecast_service,
    data_preparation_service=data_preparation_service,
    prediction_repository=prediction_repository,
)
prediction_scheduler = PredictionScheduler(prediction_service)

# Initialize power readings services
power_readings_repository = PowerReadingsRepository()
power_readings_service = PowerReadingsService(power_readings_repository)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    try:
        db_success = await db_manager.initialize()
        if not db_success:
            logging.error("Failed to initialize database connection pool")
            raise RuntimeError("Database initialization failed")

        state_manager.refresh_state()

        await prediction_scheduler.start()

    except Exception as e:
        logging.error(f"Startup error: {e}")

    yield

    # Shutdown
    try:
        # Gracefully stop the prediction scheduler first (it needs database connection)
        await prediction_scheduler.stop()

        await db_manager.close()
    except Exception as e:
        logging.error(f"Shutdown error: {e}")


app = FastAPI(title="Solar Prediction Service", version="1.0.0", lifespan=lifespan)


@app.get("/")
async def root():
    return {"message": "Solar Prediction Service is running"}


@app.get("/internal/status")
async def get_status():
    power_plants = state_manager.get_active_power_plants()
    models = []
    for power_plant in power_plants:
        models.append(state_manager.get_active_models_for_power_plant(power_plant.id))
    return {
        "service": "Solar Prediction Service",
        "power_plants": power_plants,
        "models": models,
        "prediction_scheduler": prediction_scheduler.get_status(),
    }


@app.get("/forecast/{model_id}", response_model=List[ForecastResponse])
async def get_forecast(
    model_id: int,
    start_date: datetime = Query(..., description="Start date in ISO 8601 format"),
    end_date: datetime = Query(..., description="End date in ISO 8601 format"),
):

    logging.info(
        f"Received forecast request for model {model_id}, start_date: {start_date}, end_date: {end_date}"
    )

    try:
        if start_date >= end_date:
            logging.warning(
                f"Invalid date range: start_date {start_date} >= end_date {end_date}"
            )
            raise HTTPException(
                status_code=400, detail="Start date must be before end date"
            )

        forecast_data = await prediction_repository.get_forecast_data(
            model_id, start_date, end_date
        )

        response = [
            ForecastResponse(
                id=row["id"],
                prediction_time=row["prediction_time"],
                power_output=row["power_output"],
            )
            for row in forecast_data
        ]

        return response

    except HTTPException:
        raise
    except Exception as e:
        logging.error(
            f"Error fetching forecast for model {model_id}: {e}", exc_info=True
        )
        raise HTTPException(status_code=500, detail="Failed to fetch forecast data")


@app.post("/reading/{plant_id}", response_model=CSVUploadResponse)
async def upload_power_readings(
    plant_id: int,
    file: UploadFile = File(
        ..., description="CSV file with timestamp and power columns"
    ),
):
    """
    Upload CSV file containing power readings for a specific power plant.

    CSV format:
    - No headers
    - Two columns: timestamp (ISO format), power (float)
    - Example: 2024-01-01T12:00:00Z,1234.56
    """
    logging.info(f"Received CSV upload request for plant {plant_id}")

    try:
        if not file.filename or not file.filename.lower().endswith(".csv"):
            return CSVUploadResponse(
                success=False,
                message="File must be a CSV file",
                validation_errors=["Invalid file type. Only CSV files are accepted."],
            )

        result = await power_readings_service.upload_csv_readings(file, plant_id)

        if result.success:
            return result
        else:
            raise HTTPException(
                status_code=400,
                detail={
                    "success": False,
                    "message": result.message,
                    "validation_errors": result.validation_errors,
                },
            )

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error uploading CSV for plant {plant_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to upload CSV file")
