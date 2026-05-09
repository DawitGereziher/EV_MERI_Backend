import os
import sys
import django
from django.test import Client
from django.contrib.auth import get_user_model

# Set up Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mengedmate.settings')
django.setup()

User = get_user_model()

def test_admin_pages():
    client = Client()
    
    # Create a superuser for testing
    username = 'admin_test@example.com'
    if not User.objects.filter(email=username).exists():
        admin_user = User.objects.create_superuser(email=username, password='password123')
        print(f"Created superuser: {username}")
    else:
        admin_user = User.objects.get(email=username)
        print(f"Using existing superuser: {username}")
    
    client.force_login(admin_user)
    
    urls_to_test = [
        '/admin/',
        '/admin/authentication/customuser/',
        '/admin/authentication/customuser/add/',
        '/admin/charging_stations/chargingstation/',
        '/admin/charging_stations/chargingstation/add/',
    ]
    
    for url in urls_to_test:
        print(f"Testing URL: {url}...")
        try:
            response = client.get(url)
            if response.status_code == 200:
                print(f"  OK: 200")
            elif response.status_code == 500:
                print(f"  ERROR: 500")
                # Try to find the error in the response if possible, 
                # but usually it's in the console with DEBUG=True
            else:
                print(f"  WARNING: {response.status_code}")
        except Exception as e:
            print(f"  CRASH: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    test_admin_pages()
