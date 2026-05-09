import os
import sys
import django
from django.db import connection

# Set up Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mengedmate.settings')
django.setup()

def check_model_tables():
    from django.apps import apps
    from django.db.utils import OperationalError
    
    models_to_check = [
        ('authentication', 'CustomUser'),
        ('authentication', 'Vehicle'),
        ('charging_stations', 'StationOwner'),
        ('charging_stations', 'ChargingStation'),
        ('charging_stations', 'ChargingConnector'),
    ]
    
    print("Checking database tables...")
    for app_label, model_name in models_to_check:
        model = apps.get_model(app_label, model_name)
        table_name = model._meta.db_table
        print(f"Checking {app_label}.{model_name} (table: {table_name})...")
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
                count = cursor.fetchone()[0]
                print(f"  OK: {count} records found.")
        except OperationalError as e:
            print(f"  ERROR: Table {table_name} missing or inaccessible: {e}")
        except Exception as e:
            print(f"  ERROR: Unexpected error checking {table_name}: {e}")

if __name__ == "__main__":
    try:
        check_model_tables()
    except Exception as e:
        print(f"Fatal error during check: {e}")
