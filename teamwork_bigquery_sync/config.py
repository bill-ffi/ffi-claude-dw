import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _required(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Config:
    teamwork_api_key: str
    teamwork_base_url: str
    gcp_project_id: str
    bq_dataset: str
    bq_location: str
    sync_timezone: str


def load_config():
    return Config(
        teamwork_api_key=_required("TEAMWORK_API_KEY"),
        teamwork_base_url=_required("TEAMWORK_BASE_URL").rstrip("/"),
        gcp_project_id=os.environ.get("GCP_PROJECT_ID", "radiant-rig-284611"),
        bq_dataset=os.environ.get("BQ_DATASET", "teamwork_data"),
        bq_location=os.environ.get("BQ_LOCATION", "US"),
        sync_timezone=os.environ.get("SYNC_TIMEZONE", "UTC"),
    )
