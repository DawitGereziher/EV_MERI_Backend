import os
import sys
import django

# Set up Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mengedmate.settings')
django.setup()

print("Django setup successful.")

# Try to import admin site
from django.contrib.admin.sites import site
print("Admin site loaded.")

from authentication.models import CustomUser
from django.contrib.auth import get_user_model
print("User model loaded.")

try:
    from authentication.admin import CustomUserAdmin
    print("CustomUserAdmin imported successfully.")
except Exception as e:
    print("Failed to import CustomUserAdmin:", e)
