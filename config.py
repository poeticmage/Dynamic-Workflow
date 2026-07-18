import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    """Centralized static configuration parameters.

    All model calls in this framework go through Google ADK on Gemini; the API
    key is read from the environment (see .env), never hardcoded.
    """

    GOOGLE_API_KEY: str = os.environ.get("GOOGLE_API_KEY", "")

    # gemini-2.5-flash is the sole model used everywhere in this framework
    # (planner, refiner, router, validator, summarizer, and every specialist
    # agent in agent.py) - confirmed live against the Gemini API.
    MODEL: str = "gemini-2.5-flash"
    PLANNER_MODEL: str = MODEL
    ROUTER_MODEL: str = MODEL
    VALIDATOR_MODEL: str = MODEL
    SUMMARY_MODEL: str = MODEL
    TEMPERATURE: float = 1.0

    @classmethod
    def set(cls, key: str, value):
        """Set a configuration parameter dynamically."""
        if hasattr(cls, key):
            setattr(cls, key, value)
        else:
            raise AttributeError(f"{key} is not a valid configuration attribute.")

    @classmethod
    def get(cls, key: str):
        """Retrieve a configuration parameter."""
        if hasattr(cls, key):
            return getattr(cls, key)
        else:
            raise AttributeError(f"{key} is not a valid configuration attribute.")


if not Config.GOOGLE_API_KEY:
    raise RuntimeError(
        "GOOGLE_API_KEY is not set. Add it to a local .env file "
        "(GOOGLE_API_KEY=...) before running Flow."
    )
