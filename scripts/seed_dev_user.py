"""Seed a dev user, 2012 LR4 vehicle, and sample searches for local testing.

Run after migrations:
    PYTHONPATH=$PWD python scripts/seed_dev_user.py
"""

import sys
import os

# Ensure project root is on the Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.models import Search, User, Vehicle
from app.db.session import get_session

SAMPLE_SEARCHES = [
    {"query_text": "LR4 coolant crossover pipe", "oem_number": "LR010819"},
    {"query_text": "LR4 water pump", "oem_number": "LR033993"},
    {"query_text": "LR4 timing chain kit", "oem_number": None},
    {"query_text": "LR4 fuel injector", "oem_number": "LR079542"},
    {"query_text": "LR4 thermostat housing", "oem_number": "LR005631"},
]


def seed() -> None:
    with get_session() as db:
        # Check if user already exists
        user = db.query(User).filter(User.email == "admin").first()
        if user:
            print(f"User 'admin' already exists (id={user.id}). Skipping.")
            return

        user = User(email="admin")
        db.add(user)
        db.flush()  # Get user.id assigned

        vehicle = Vehicle(
            user_id=user.id,
            year=2012,
            make="Land Rover",
            model="LR4",
            nickname="The Beast",
        )
        db.add(vehicle)
        db.flush()  # Get vehicle.id assigned

        for search_data in SAMPLE_SEARCHES:
            search = Search(
                user_id=user.id,
                vehicle_id=vehicle.id,
                query_text=search_data["query_text"],
                oem_number=search_data["oem_number"],
                is_active=True,
                is_high_priority=False,
            )
            db.add(search)

        # Commit happens automatically via get_session context manager
        print(
            f"Seeded: user={user.id}, vehicle={vehicle.id}, "
            f"{len(SAMPLE_SEARCHES)} searches"
        )


if __name__ == "__main__":
    seed()
