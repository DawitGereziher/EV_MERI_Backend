import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mengedmate.settings')
django.setup()

from payments.serializers import QRPaymentInitiateSerializer

data = {
    'payment_type': 'amount',
    'phone_number': '0900123456',
    'amount': 1000.0
}

serializer = QRPaymentInitiateSerializer(data=data)
if serializer.is_valid():
    print("Serializer is valid!")
    print("Validated data:", serializer.validated_data)
else:
    print("Serializer is INVALID!")
    print("Errors:", serializer.errors)
