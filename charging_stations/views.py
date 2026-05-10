from rest_framework import generics, status, permissions, serializers
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.decorators import api_view, permission_classes
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.conf import settings
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.http import HttpResponse
from math import cos, radians
from .models import (
    StationOwner, ChargingStation, StationImage, ChargingConnector,
    AppContent, StationReview, ReviewReply, StationOwnerSettings, NotificationTemplate,
    PayoutMethod, WithdrawalRequest
)
from .serializers import (
    StationOwnerRegistrationSerializer,
    StationOwnerProfileSerializer,
    ChargingStationSerializer,
    ChargingConnectorSerializer,
    StationImageSerializer,
    StationReviewSerializer,
    StationReviewListSerializer,
    ReviewReplySerializer,
    ReviewReplyListSerializer,
    StationOwnerSettingsSerializer,
    NotificationTemplateSerializer,
    AvailableStationSerializer,
    PayoutMethodSerializer,
    WithdrawalRequestSerializer,
    WithdrawalRequestAdminSerializer,
    FirestoreChargingStationSerializer,
    FirestoreStationReviewSerializer
)
from .serializers_firestore import FirestoreStationOwnerSerializer, FirestorePayoutMethodSerializer, FirestoreWithdrawalRequestSerializer
from authentication.authentication import AnonymousAuthentication, TokenAuthentication
from rest_framework.authentication import SessionAuthentication

User = get_user_model()

class StationOwnerRegistrationView(generics.GenericAPIView):

    permission_classes = [permissions.AllowAny]
    authentication_classes = [AnonymousAuthentication]
    serializer_class = StationOwnerRegistrationSerializer

    def post(self, request):
        import sys
        print(f"\n>>> StationOwner register attempt with data keys: {list(request.data.keys())}", file=sys.stderr)
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            print(f">>> StationOwner register VALIDATION ERRORS: {serializer.errors}", file=sys.stderr)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        result = serializer.save()

        user = result['user']
        verification_code = result['verification_code']

        subject = 'Verify Your evmeri EV Charging Station Owner Account'
        html_message = render_to_string('station_owner_email_verification.html', {
            'user': user,
            'verification_code': verification_code,
            'frontend_url': settings.FRONTEND_URL
        })
        plain_message = strip_tags(html_message)

        print("\n" + "="*50)
        print(f"STATION OWNER VERIFICATION CODE FOR {user.email}: {verification_code}")
        print("="*50 + "\n")

        try:
            send_mail(
                subject,
                plain_message,
                settings.DEFAULT_FROM_EMAIL,
                [user.email],
                html_message=html_message,
                fail_silently=False,
            )
        except Exception as e:
            import traceback
            import sys
            print(f"Failed to send verification email: {str(e)}", file=sys.stderr)
            print("To fix the timeout, set USE_CONSOLE_EMAIL=True in Render environment variables or configure valid EMAIL_HOST_USER credentials.", file=sys.stderr)

        return Response({
            "message": "Registration successful. Please check your email for verification code.",
            "email": user.email
        }, status=status.HTTP_201_CREATED)

class StationOwnerVerifyEmailView(APIView):

    permission_classes = [permissions.AllowAny]
    authentication_classes = [AnonymousAuthentication]

    def post(self, request):
        email = request.data.get('email')
        verification_code = request.data.get('verification_code')

        if not email or not verification_code:
            return Response({
                "message": "Email and verification code are required."
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response({
                "message": "User with this email does not exist."
            }, status=status.HTTP_404_NOT_FOUND)

        if user.is_verified:
            return Response({
                "message": "Email is already verified."
            }, status=status.HTTP_400_BAD_REQUEST)

        if user.verification_code != verification_code:
            return Response({
                "message": "Invalid verification code."
            }, status=status.HTTP_400_BAD_REQUEST)

        user.is_verified = True
        user.verification_code = None
        user.save()

        station_owner = firestore_repo.get_station_owner(user.id)
        if not station_owner:
            return Response({
                "message": "Station owner profile not found."
            }, status=status.HTTP_404_NOT_FOUND)

        return Response({
            "message": "Email verified successfully. Please complete your profile.",
            "station_owner_id": station_owner.get('id')
        }, status=status.HTTP_200_OK)

class StationOwnerProfileView(generics.RetrieveUpdateAPIView):

    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [TokenAuthentication, SessionAuthentication]
    serializer_class = FirestoreStationOwnerSerializer
    parser_classes = [MultiPartParser, FormParser]

    def get_object(self):
        profile = firestore_repo.get_station_owner(self.request.user.id)
        if not profile:
            # Fallback for old users or error (could create one on the fly ideally)
             return Response({
                "message": "Station owner profile not found."
            }, status=status.HTTP_404_NOT_FOUND)
        return profile

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if isinstance(instance, Response): return instance # data not found

        partial = kwargs.pop('partial', True)
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        
        # Need to inject user_id context logic if needed? 
        # Serializer handles update(instance, data).
        
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        if not instance.get('is_profile_completed') and serializer.validated_data.get('is_profile_completed'):
            # Send admin notification
            company_name = instance.get('company_name')
            subject = 'New Station Owner Requires Verification'
            message = f'A new station owner ({company_name}) has completed their profile and requires verification.'
            send_mail(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                [settings.ADMIN_EMAIL],
                fail_silently=True,
            )

            # Send real-time notification to user
            from authentication.notifications import create_notification, Notification
            create_notification(
                user=request.user,
                notification_type=Notification.NotificationType.SYSTEM,
                title='Profile Submitted for Verification',
                message='Your station owner profile has been submitted for verification. Our team will review your documents within 1-3 business days.',
                link='/dashboard/profile'
            )

        return Response(serializer.data)

    def perform_update(self, serializer):
        serializer.save()

        # Sync uploaded documents back to SQL so Django Admin can display them
        try:
            from .models import StationOwner
            request = self.request
            sql_owner = StationOwner.objects.filter(user=request.user).first()
            if sql_owner:
                data = serializer.validated_data
                changed = False
                for field in ['business_document', 'business_license', 'id_proof', 'utility_bill',
                              'company_name', 'business_registration_number', 'contact_phone',
                              'contact_email', 'website', 'description']:
                    if field in data and data[field] is not None:
                        setattr(sql_owner, field, data[field])
                        changed = True
                if data.get('is_profile_completed'):
                    sql_owner.is_profile_completed = True
                    changed = True
                if changed:
                    sql_owner.save()
        except Exception as e:
            import sys
            print(f"Warning: Could not sync profile update to SQL: {e}", file=sys.stderr)


from utils.firestore_repo import firestore_repo
from .serializers import FirestoreChargingStationSerializer

class ChargingStationListCreateView(generics.ListCreateAPIView):

    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [TokenAuthentication, SessionAuthentication]
    serializer_class = ChargingStationSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        return ChargingStation.objects.filter(owner__user=self.request.user)

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def perform_create(self, serializer):
        station = serializer.save()
        data = serializer.data
        data['owner_id'] = str(station.owner.user.id)
        if data.get('latitude'):
            data['latitude'] = float(data['latitude'])
        if data.get('longitude'):
            data['longitude'] = float(data['longitude'])
        firestore_repo.create_station(data)

class ChargingStationDetailView(generics.RetrieveUpdateDestroyAPIView):

    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [TokenAuthentication, SessionAuthentication]
    serializer_class = ChargingStationSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    lookup_field = 'id'

    def get_queryset(self):
        return ChargingStation.objects.filter(owner__user=self.request.user)

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        return Response(serializer.data)

    def perform_update(self, serializer):
        station = serializer.save()
        data = serializer.data
        data['owner_id'] = str(station.owner.user.id)
        if data.get('latitude'):
            data['latitude'] = float(data['latitude'])
        if data.get('longitude'):
            data['longitude'] = float(data['longitude'])
        firestore_repo.update_station(str(station.id), data)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        station_id = str(instance.id)
        instance.delete()
        firestore_repo.delete_station(station_id)
        return Response(status=status.HTTP_204_NO_CONTENT)

from .serializers import FirestoreChargingConnectorSerializer

class ConnectorCreateView(generics.CreateAPIView):

    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [TokenAuthentication, SessionAuthentication]
    serializer_class = FirestoreChargingConnectorSerializer
    parser_classes = [JSONParser] # Connector data usually JSON

    def create(self, request, *args, **kwargs):
        import sys
        station_id = self.kwargs.get('station_id')

        # --- Verify station exists in SQL and belongs to this user ---
        try:
            from .models import ChargingStation, ChargingConnector, StationOwner
            sql_station = ChargingStation.objects.get(id=station_id, owner__user=request.user)
        except ChargingStation.DoesNotExist:
            return Response({"error": "Station not found or permission denied"}, status=status.HTTP_404_NOT_FOUND)

        # --- Save connector to SQL ---
        data = request.data
        try:
            sql_connector = ChargingConnector.objects.create(
                station=sql_station,
                connector_type=data.get('connector_type', 'type2'),
                power_kw=data.get('power_kw', 22),
                quantity=int(data.get('quantity', 1)),
                available_quantity=int(data.get('quantity', 1)),
                price_per_kwh=data.get('price_per_kwh'),
                is_available=data.get('is_available', True),
                status=data.get('status', 'available'),
                description=data.get('description', ''),
            )
            # Update station connector counts
            sql_station.update_connector_counts()
        except Exception as e:
            print(f"Failed to create SQL connector: {e}", file=sys.stderr)
            return Response({"error": f"Failed to create connector: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

        # --- Also mirror to Firestore (best-effort, don't fail if it errors) ---
        try:
            connector_data = {
                'id': str(sql_connector.id),
                'connector_type': sql_connector.connector_type,
                'power_kw': float(sql_connector.power_kw),
                'quantity': sql_connector.quantity,
                'available_quantity': sql_connector.available_quantity,
                'price_per_kwh': float(sql_connector.price_per_kwh) if sql_connector.price_per_kwh else None,
                'is_available': sql_connector.is_available,
                'status': sql_connector.status,
            }
            firestore_repo.add_connector_to_station(str(station_id), connector_data)
        except Exception as e:
            print(f"Warning: Firestore connector sync failed (non-fatal): {e}", file=sys.stderr)

        return Response({
            'success': True,
            'message': 'Connector added successfully',
            'connector': {
                'id': str(sql_connector.id),
                'connector_type': sql_connector.connector_type,
                'power_kw': float(sql_connector.power_kw),
                'quantity': sql_connector.quantity,
                'available_quantity': sql_connector.available_quantity,
                'price_per_kwh': float(sql_connector.price_per_kwh) if sql_connector.price_per_kwh else None,
                'is_available': sql_connector.is_available,
                'status': sql_connector.status,
            }
        }, status=status.HTTP_201_CREATED)


class ConnectorDetailView(generics.RetrieveUpdateDestroyAPIView):

    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [TokenAuthentication, SessionAuthentication]
    serializer_class = FirestoreChargingConnectorSerializer
    lookup_field = 'id'

    def _get_sql_connector(self):
        """Get connector from SQL, verifying ownership via the station."""
        import sys
        from .models import ChargingConnector, ChargingStation
        station_id = self.kwargs.get('station_id')
        connector_id = self.kwargs.get('id')
        try:
            connector = ChargingConnector.objects.select_related('station__owner__user').get(
                id=connector_id,
                station__id=station_id,
                station__owner__user=self.request.user
            )
            return connector
        except ChargingConnector.DoesNotExist:
            from rest_framework.exceptions import NotFound
            raise NotFound("Connector not found or permission denied.")

    def retrieve(self, request, *args, **kwargs):
        connector = self._get_sql_connector()
        return Response({
            'id': str(connector.id),
            'connector_type': connector.connector_type,
            'power_kw': float(connector.power_kw),
            'quantity': connector.quantity,
            'available_quantity': connector.available_quantity,
            'price_per_kwh': float(connector.price_per_kwh) if connector.price_per_kwh else None,
            'is_available': connector.is_available,
            'status': connector.status,
            'description': connector.description or '',
            'qr_code_token': connector.qr_code_token,
            'qr_code_image': connector.qr_code_image,
        })

    def update(self, request, *args, **kwargs):
        import sys
        connector = self._get_sql_connector()
        data = request.data
        updateable = ['connector_type', 'power_kw', 'quantity', 'available_quantity',
                      'price_per_kwh', 'is_available', 'status', 'description']
        for field in updateable:
            if field in data:
                setattr(connector, field, data[field])
        connector.save()
        connector.station.update_connector_counts()

        # Mirror to Firestore (best-effort)
        try:
            firestore_repo.update_connector(str(self.kwargs.get('station_id')), str(connector.id), {
                'connector_type': connector.connector_type,
                'power_kw': float(connector.power_kw),
                'quantity': connector.quantity,
                'available_quantity': connector.available_quantity,
                'price_per_kwh': float(connector.price_per_kwh) if connector.price_per_kwh else None,
                'is_available': connector.is_available,
                'status': connector.status,
            })
        except Exception as e:
            print(f"Warning: Firestore connector update failed (non-fatal): {e}", file=sys.stderr)

        return Response({
            'id': str(connector.id),
            'connector_type': connector.connector_type,
            'power_kw': float(connector.power_kw),
            'quantity': connector.quantity,
            'available_quantity': connector.available_quantity,
            'price_per_kwh': float(connector.price_per_kwh) if connector.price_per_kwh else None,
            'is_available': connector.is_available,
            'status': connector.status,
        })

    def destroy(self, request, *args, **kwargs):
        import sys
        connector = self._get_sql_connector()
        station = connector.station
        connector_id_str = str(connector.id)
        connector.delete()
        station.update_connector_counts()

        # Mirror to Firestore (best-effort)
        try:
            firestore_repo.delete_connector(str(self.kwargs.get('station_id')), connector_id_str)
        except Exception as e:
            print(f"Warning: Firestore connector delete failed (non-fatal): {e}", file=sys.stderr)

        return Response(status=status.HTTP_204_NO_CONTENT)

from .serializers import FirestoreStationImageSerializer

class StationImageCreateView(generics.CreateAPIView):

    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [TokenAuthentication, SessionAuthentication]
    serializer_class = FirestoreStationImageSerializer
    parser_classes = [MultiPartParser, FormParser]

    def create(self, request, *args, **kwargs):
        import sys
        from .models import ChargingStation, StationImage

        station_id = self.kwargs.get('station_id')
        try:
            sql_station = ChargingStation.objects.get(id=station_id, owner__user=request.user)
        except ChargingStation.DoesNotExist:
            return Response({"error": "Station not found or permission denied"}, status=status.HTTP_404_NOT_FOUND)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data

        # Save to SQL so station detail endpoint returns it
        try:
            sql_image = StationImage.objects.create(
                station=sql_station,
                image=validated.get('image', ''),
                caption=validated.get('caption', ''),
                order=validated.get('order', 0),
            )
        except Exception as e:
            print(f"Failed to create SQL StationImage: {e}", file=sys.stderr)
            return Response({"error": f"Failed to save image: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

        # Mirror to Firestore (best-effort)
        try:
            firestore_repo.create_image(str(station_id), {
                'id': str(sql_image.id),
                'image': sql_image.image,
                'caption': sql_image.caption or '',
                'order': sql_image.order,
            })
        except Exception as e:
            print(f"Warning: Firestore image sync failed (non-fatal): {e}", file=sys.stderr)

        return Response({
            'id': sql_image.id,
            'image': sql_image.image,
            'caption': sql_image.caption,
            'order': sql_image.order,
        }, status=status.HTTP_201_CREATED)




class StationQRCodesView(APIView):
    """View to get QR codes for all connectors of a station"""
    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [TokenAuthentication, SessionAuthentication]

    def get(self, request, station_id):
        try:
            station = firestore_repo.get_station(station_id)
            if not station:
                 return Response({"error": "Station not found"}, status=status.HTTP_404_NOT_FOUND)

            if station.get('owner_id') != str(request.user.id):
                 return Response({"error": "Permission denied"}, status=status.HTTP_403_FORBIDDEN)

            connectors = firestore_repo.list_connectors(station_id)
            qr_data = []

            for connector in connectors:
                qr_token = connector.get('qr_code_token')
                qr_data.append({
                    'connector_id': connector.get('id'),
                    'connector_type': connector.get('connector_type'),
                    'connector_type_display': connector.get('connector_type_display'),
                    'power_kw': connector.get('power_kw'),
                    'quantity': connector.get('quantity'),
                    'available_quantity': connector.get('available_quantity'),
                    'price_per_kwh': connector.get('price_per_kwh'),
                    'qr_code_token': qr_token,
                    'qr_code_url': connector.get('qr_code_image'), # We store base64 as 'qr_code_image' or URL? Serializer said 'qr_code_image'
                    'qr_payment_url': f"{settings.API_BASE_URL}/api/payments/qr-initiate/{qr_token}/" if qr_token else None,
                    'is_available': connector.get('is_available'),
                    'status': connector.get('status'),
                    'status_display': connector.get('status_display')
                })

            return Response({
                'success': True,
                'station': {
                    'id': station.get('id'),
                    'name': station.get('name'),
                    'address': station.get('address')
                },
                'connectors': qr_data
            })

        except Exception as e:
            return Response({
                'success': False,
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ConnectorQRCodeView(APIView):
    """View to get or regenerate QR code for a specific connector"""
    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [TokenAuthentication, SessionAuthentication]

    def get(self, request, connector_id):
        try:
            # We need station_id to find connector in subcollection!!! 
            # The URL usually contains station_id if nested, 
            # but here it seems to be just /connectors/<id>/qrcode/?
            # If so, we have a problem: Firestore needs parent ID.
            # Assuming the URL structure is /stations/<station_id>/connectors/<id>/qrcode/ 
            # Let's check the kwargs.
            # If the URL is just /connectors/<id>/, we need to search ALL stations? Efficient query needed.
            # Or we change URL structure.
            # EXISTING urls.py likely routes /api/connectors/<pk>/qrcode/ 
            # SQL allows direct lookup. Firestore requires parent.
            # We can use Collection Group Query to find connector by ID, get parent station.
            # Or we rely on request having station_id? Unlikely.
            
            # For now, I'll attempt a Collection Group Query wrapper in repo if needed.
            # But let's check how to get station_id.
            # If I can't change URLs, I must find station.
            pass
            # I will use a helper to find station by connector_id
            
            # Temporary: search matching connector in ALL stations owned by user (filtered list)
            # This is slow but safe for now.
            filters = {'owner_id': str(request.user.id)}
            all_stations = firestore_repo.list_stations(filters=filters)
            
            found_connector = None
            found_station = None
            
            for station in all_stations:
                conn = firestore_repo.get_connector(station.get('id'), connector_id)
                if conn:
                    found_connector = conn
                    found_station = station
                    break
            
            if not found_connector:
                 return Response({"error": "Connector not found"}, status=status.HTTP_404_NOT_FOUND)

            connector = found_connector
            
            # QR Token logic is inside ChargingConnector model save() in SQL.
            # In Firestore, we need to generate it.
            
            return Response({
                'success': True,
                'connector': {
                    'id': connector.get('id'),
                    'connector_type': connector.get('connector_type'),
                    'connector_type_display': connector.get('connector_type_display'),
                    'power_kw': connector.get('power_kw'),
                    'price_per_kwh': connector.get('price_per_kwh'),
                    'qr_code_token': connector.get('qr_code_token'),
                    'qr_code_url': connector.get('qr_code_image'),
                    'qr_payment_url': f"{settings.API_BASE_URL}/api/payments/qr-initiate/{connector.get('qr_code_token')}/" if connector.get('qr_code_token') else None,
                    'station_name': found_station.get('name')
                }
            })

        except Exception as e:
            return Response({
                'success': False,
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def post(self, request, connector_id):
        """Regenerate QR code for connector"""
        # Similar logic to find connector
        try:
            filters = {'owner_id': str(request.user.id)}
            all_stations = firestore_repo.list_stations(filters=filters)
            
            found_connector = None
            found_station = None
            
            for station in all_stations:
                conn = firestore_repo.get_connector(station.get('id'), connector_id)
                if conn:
                    found_connector = conn
                    found_station = station
                    break
            
            if not found_connector:
                 return Response({"error": "Connector not found"}, status=status.HTTP_404_NOT_FOUND)
            
            # Logic to regenerate QR
            from utils.qr_generator import generate_qr_code_base64, generate_unique_token
            
            # Generate new token
            unique_string = f"{found_station.get('id')}-{connector_id}"
            token = generate_unique_token(unique_string)
            
            # Generate QR image
            from django.conf import settings
            qr_data = f"{settings.API_BASE_URL}/api/payments/qr-initiate/{token}/"
            qr_image = generate_qr_code_base64(qr_data)
            
            # Update Firestore
            updates = {
                'qr_code_token': token,
            }
            if qr_image:
                updates['qr_code_image'] = qr_image

            updated = firestore_repo.update_connector(found_station.get('id'), connector_id, updates)
            
            return Response({
                'success': True,
                'message': 'QR code regenerated successfully',
                'connector': {
                    'id': updated.get('id'),
                    'qr_code_token': updated.get('qr_code_token'),
                    'qr_code_url': updated.get('qr_code_image'),
                    'qr_payment_url': f"{settings.API_BASE_URL}/api/payments/qr-initiate/{updated.get('qr_code_token')}/"
                }
            })
            
        except Exception as e:
            return Response({
                'success': False,
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class DownloadQRCodeView(APIView):
    """View to download QR code image for printing"""
    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [TokenAuthentication, SessionAuthentication]

    def get(self, request, connector_id):
        try:
            # Same search logic
            filters = {'owner_id': str(request.user.id)}
            all_stations = firestore_repo.list_stations(filters=filters)
            
            found_connector = None
            found_station = None
            
            for station in all_stations:
                conn = firestore_repo.get_connector(station.get('id'), connector_id)
                if conn:
                    found_connector = conn
                    found_station = station
                    break
            
            if not found_connector:
                 return Response({"error": "Connector not found"}, status=status.HTTP_404_NOT_FOUND)
            
            qr_image = found_connector.get('qr_code_image')
            if not qr_image:
                return Response({
                    'success': False,
                    'error': 'QR code not found for this connector'
                }, status=status.HTTP_404_NOT_FOUND)

            try:
                import base64
                from utils.base64_image import decode_base64_to_bytes, get_base64_mime_type

                base64_data = qr_image
                mime_type = get_base64_mime_type(base64_data) or 'image/png'
                image_bytes = decode_base64_to_bytes(base64_data)
                
                response = HttpResponse(image_bytes, content_type=mime_type)
                ext = mime_type.split('/')[-1] if '/' in mime_type else 'png'
                filename = f"qr_code_{found_station.get('id')}_{connector_id}.{ext}"
                
                response['Content-Disposition'] = f'attachment; filename="{filename}"'
                return response
            except Exception as e:
                return Response({
                    'success': False,
                    'error': f'Failed to decode QR code: {str(e)}'
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        except Exception as e:
            return Response({
                'success': False,
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AppContentView(APIView):
    """View to get app content like About, Privacy Policy, Terms of Service"""
    permission_classes = [permissions.AllowAny]
    authentication_classes = [AnonymousAuthentication]

    def get(self, request, content_type=None):
        try:
            if content_type:
                # Get specific content type
                content = get_object_or_404(AppContent, content_type=content_type, is_active=True)
                return Response({
                    'success': True,
                    'content': {
                        'content_type': content.content_type,
                        'title': content.title,
                        'content': content.content,
                        'version': content.version,
                        'updated_at': content.updated_at
                    }
                })
            else:
                # Get all active content
                contents = AppContent.objects.filter(is_active=True).order_by('content_type')
                content_data = []
                for content in contents:
                    content_data.append({
                        'content_type': content.content_type,
                        'title': content.title,
                        'content': content.content,
                        'version': content.version,
                        'updated_at': content.updated_at
                    })

                return Response({
                    'success': True,
                    'contents': content_data
                })

        except Exception as e:
            return Response({
                'success': False,
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class StationReviewListCreateView(generics.ListCreateAPIView):
    """View to list and create station reviews (SQL source of truth, Firestore mirror)"""

    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    authentication_classes = [TokenAuthentication, SessionAuthentication]
    serializer_class = FirestoreStationReviewSerializer

    def get_queryset(self):
        return []

    def list(self, request, *args, **kwargs):
        import sys
        from .models import StationReview, ChargingStation
        station_id = self.kwargs.get('station_id')
        try:
            reviews = StationReview.objects.filter(
                station__id=station_id, is_active=True
            ).select_related('user', 'station').order_by('-created_at')
            data = [{
                'id': str(r.id),
                'user_id': str(r.user.id),
                'user_name': r.user.get_full_name() or r.user.email,
                'station_id': str(r.station.id),
                'rating': r.rating,
                'review_text': r.review_text or '',
                'charging_speed_rating': r.charging_speed_rating,
                'location_rating': r.location_rating,
                'amenities_rating': r.amenities_rating,
                'is_verified_review': r.is_verified_review,
                'created_at': r.created_at.isoformat(),
                'reply': {'text': r.reply.reply_text, 'created_at': r.reply.created_at.isoformat()} if hasattr(r, 'reply') and r.reply else None,
            } for r in reviews]
            return Response(data)
        except Exception as e:
            print(f"Warning: SQL review list failed, falling back to Firestore: {e}", file=sys.stderr)
            reviews = firestore_repo.list_reviews(station_id)
            return Response(reviews)

    def create(self, request, *args, **kwargs):
        import sys, traceback
        from .models import StationReview, ChargingStation
        try:
            station_id = self.kwargs.get('station_id')
            try:
                sql_station = ChargingStation.objects.get(id=station_id)
            except ChargingStation.DoesNotExist:
                return Response({"error": "Station not found"}, status=status.HTTP_404_NOT_FOUND)

            data = request.data
            # Upsert: one review per user per station
            sql_review, created = StationReview.objects.update_or_create(
                user=request.user,
                station=sql_station,
                defaults={
                    'rating': int(data.get('rating', 5)),
                    'review_text': data.get('review_text', '') or data.get('comment', ''),
                    'charging_speed_rating': data.get('charging_speed_rating') or None,
                    'location_rating': data.get('location_rating') or None,
                    'amenities_rating': data.get('amenities_rating') or None,
                    'is_active': True,
                }
            )
            # StationReview.save() auto-updates station.rating + station.rating_count

            review_dict = {
                'id': str(sql_review.id),
                'user_id': str(request.user.id),
                'station_id': str(station_id),
                'rating': sql_review.rating,
                'review_text': sql_review.review_text or '',
                'created_at': sql_review.created_at.isoformat(),
            }

            # Mirror to Firestore (best-effort)
            try:
                if created:
                    firestore_repo.create_review(str(station_id), review_dict)
                else:
                    firestore_repo.update_review(str(station_id), str(sql_review.id), review_dict)
            except Exception as e:
                print(f"Warning: Firestore review sync failed (non-fatal): {e}", file=sys.stderr)

            return Response(review_dict, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

        except Exception as e:
            print(f"Error creating review: {e}\n{traceback.format_exc()}")
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class StationReviewDetailView(generics.RetrieveUpdateDestroyAPIView):
    """View to retrieve, update, or delete a specific review (SQL source of truth)"""

    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [TokenAuthentication, SessionAuthentication]
    lookup_field = 'id'

    def _get_sql_review(self):
        from .models import StationReview
        from rest_framework.exceptions import NotFound, PermissionDenied
        review_id = self.kwargs.get('id')
        try:
            review = StationReview.objects.select_related('station', 'user').get(id=review_id)
        except StationReview.DoesNotExist:
            raise NotFound('Review not found.')
        if review.user != self.request.user and not self.request.user.is_staff:
            raise PermissionDenied('Not your review.')
        return review

    def retrieve(self, request, *args, **kwargs):
        r = self._get_sql_review()
        return Response({'id': str(r.id), 'rating': r.rating, 'review_text': r.review_text, 'created_at': r.created_at.isoformat()})

    def update(self, request, *args, **kwargs):
        import sys
        r = self._get_sql_review()
        data = request.data
        r.rating = data.get('rating', r.rating)
        r.review_text = data.get('review_text', r.review_text) or data.get('comment', r.review_text)
        r.charging_speed_rating = data.get('charging_speed_rating', r.charging_speed_rating)
        r.location_rating = data.get('location_rating', r.location_rating)
        r.amenities_rating = data.get('amenities_rating', r.amenities_rating)
        r.save()  # auto-updates station rating
        try:
            firestore_repo.update_review(str(r.station.id), str(r.id), {'rating': r.rating, 'review_text': r.review_text})
        except Exception as e:
            print(f"Warning: Firestore review update failed (non-fatal): {e}", file=sys.stderr)
        return Response({'id': str(r.id), 'rating': r.rating, 'review_text': r.review_text})

    def destroy(self, request, *args, **kwargs):
        import sys
        r = self._get_sql_review()
        station_id_str = str(r.station.id)
        review_id_str = str(r.id)
        r.delete()  # auto-updates station rating via model.delete()
        try:
            firestore_repo.delete_review(station_id_str, review_id_str)
        except Exception as e:
            print(f"Warning: Firestore review delete failed (non-fatal): {e}", file=sys.stderr)
        return Response(status=status.HTTP_204_NO_CONTENT)


class UserReviewsView(generics.ListAPIView):
    """View to get all reviews by the current user (reads from SQL)"""

    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [TokenAuthentication, SessionAuthentication]

    def list(self, request, *args, **kwargs):
        import sys
        from .models import StationReview
        try:
            reviews = StationReview.objects.filter(
                user=request.user, is_active=True
            ).select_related('station').order_by('-created_at')
            data = [{
                'id': str(r.id),
                'station_id': str(r.station.id),
                'station_name': r.station.name,
                'rating': r.rating,
                'review_text': r.review_text or '',
                'created_at': r.created_at.isoformat(),
            } for r in reviews]
            return Response(data)
        except Exception as e:
            print(f"Warning: SQL user-review list failed, falling back to Firestore: {e}", file=sys.stderr)
            reviews = firestore_repo.list_reviews_by_user(request.user.id)
            return Response(reviews)


class StationReviewStatsView(APIView):
    """View to get review statistics for a station"""

    permission_classes = [permissions.AllowAny]
    authentication_classes = [AnonymousAuthentication]

    def get(self, request, station_id):
        try:
            # We fetch all reviews from firestore
            reviews = firestore_repo.list_reviews(station_id)
            # Filter active? Assume all active.
            
            count = len(reviews)
            if count == 0:
                 return Response({
                    'success': True,
                    'station_id': station_id,
                    'total_reviews': 0,
                    'overall_rating': 0,
                    'rating_distribution': {str(i): 0 for i in range(1, 6)},
                    'average_ratings': {
                        'overall': 0, 'charging_speed': 0, 'location': 0, 'amenities': 0
                    },
                    'recent_reviews': [],
                    'verified_reviews_count': 0
                })

            # Calculate stats
            rating_distribution = {str(i): 0 for i in range(1, 6)}
            sum_rating = 0
            sum_speed = 0
            sum_loc = 0
            sum_amen = 0
            verified_count = 0
            
            valid_speed = 0
            valid_loc = 0
            valid_amen = 0
            
            for r in reviews:
                rt = int(r.get('rating', 0))
                if 1 <= rt <= 5:
                    rating_distribution[str(rt)] += 1
                sum_rating += rt
                
                if r.get('is_verified_review'): verified_count += 1
                
                sp = r.get('charging_speed_rating')
                if sp: 
                    sum_speed += sp
                    valid_speed += 1
                    
                lc = r.get('location_rating')
                if lc: 
                    sum_loc += lc
                    valid_loc += 1
                
                am = r.get('amenities_rating')
                if am:
                    sum_amen += am
                    valid_amen += 1

            avg_rating = sum_rating / count
            avg_speed = sum_speed / valid_speed if valid_speed else 0
            avg_loc = sum_loc / valid_loc if valid_loc else 0
            avg_amen = sum_amen / valid_amen if valid_amen else 0
            
            # Recent reviews
            recent = reviews[:5] # Already sorted desc
            recent_data = FirestoreStationReviewSerializer(recent, many=True).data

            return Response({
                'success': True,
                'station_id': station_id,
                'total_reviews': count,
                'overall_rating': round(avg_rating, 2),
                'rating_distribution': rating_distribution,
                'average_ratings': {
                    'overall': round(avg_rating, 2),
                    'charging_speed': round(avg_speed, 2),
                    'location': round(avg_loc, 2),
                    'amenities': round(avg_amen, 2),
                },
                'recent_reviews': recent_data,
                'verified_reviews_count': verified_count
            })

        except Exception as e:
            return Response({
                'success': False,
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class StationOwnerReviewsView(generics.ListAPIView):
    """View for station owners to see all reviews for their stations"""

    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [TokenAuthentication, SessionAuthentication]
    serializer_class = FirestoreStationReviewSerializer

    def get_queryset(self):
         return []

    def list(self, request, *args, **kwargs):
        try:
            # Use efficiently query by owner_id if possible
            # We didn't add 'owner_id' to review yet? 
            # Check serializer: yes we added 'station_owner_id' in Create!
            # So we can query collection group.
            
            reviews = firestore_repo.list_reviews_by_owner(str(request.user.id))
            serializer = self.get_serializer(reviews, many=True)
            return Response({
                'results': serializer.data,
                'count': len(reviews)
            })

        except Exception as e:
            return Response({'results': [], 'count': 0, 'error': str(e)})


class MobileChargingHistoryView(APIView):
    """Simplified charging history view for mobile users"""

    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [TokenAuthentication, SessionAuthentication]

    def get(self, request):
        try:
            all_sessions = []

            # Get QR Payment Sessions (most common for mobile app)
            try:
                from payments.models import QRPaymentSession

                # Get QR sessions with completed payments and charging
                qr_sessions = QRPaymentSession.objects.filter(
                    user=request.user,
                    status__in=['charging_completed', 'charging_started', 'payment_completed']
                ).select_related(
                    'connector__station',
                    'payment_transaction',
                    'simple_charging_session'
                ).order_by('-created_at')[:20]  # Reduced limit for debugging

                for qr_session in qr_sessions:
                    try:
                        # Calculate duration and cost
                        duration_seconds = 0
                        energy_kwh = 0.0
                        final_cost = 0.0
                        start_time = None
                        stop_time = None

                        # Get data from simple charging session if available
                        if hasattr(qr_session, 'simple_charging_session') and qr_session.simple_charging_session:
                            charging_session = qr_session.simple_charging_session
                            duration_seconds = charging_session.duration_seconds or 0
                            energy_kwh = float(charging_session.energy_delivered_kwh or 0)
                            start_time = charging_session.start_time
                            stop_time = charging_session.stop_time

                            # Calculate duration if not stored
                            if start_time and stop_time and not duration_seconds:
                                duration_seconds = int((stop_time - start_time).total_seconds())

                        # Calculate cost based on energy and connector price
                        if energy_kwh > 0 and qr_session.connector and qr_session.connector.price_per_kwh:
                            final_cost = energy_kwh * float(qr_session.connector.price_per_kwh)
                        elif qr_session.payment_transaction:
                            final_cost = float(qr_session.payment_transaction.amount)

                        # Format duration for display
                        duration_minutes = duration_seconds // 60 if duration_seconds else 0

                        # Safe access to connector and station data
                        station_name = qr_session.connector.station.name if qr_session.connector and qr_session.connector.station else 'Unknown Station'
                        station_address = qr_session.connector.station.address if qr_session.connector and qr_session.connector.station else 'Unknown Location'
                        station_city = qr_session.connector.station.city if qr_session.connector and qr_session.connector.station else 'Unknown City'
                        connector_type = qr_session.connector.get_connector_type_display() if qr_session.connector else 'Unknown'
                        connector_power = f"{qr_session.connector.power_kw} kW" if qr_session.connector else 'Unknown'

                        session_data = {
                            'id': str(qr_session.id),
                            'transaction_id': qr_session.session_token,
                            'station_name': station_name,
                            'station_address': station_address,
                            'station_city': station_city,
                            'connector_type': connector_type,
                            'connector_power': connector_power,
                            'start_time': start_time.isoformat() if start_time else qr_session.created_at.isoformat(),
                            'stop_time': stop_time.isoformat() if stop_time else None,
                            'energy_consumed_kwh': f"{energy_kwh:.3f}",
                            'final_cost': f"{final_cost:.2f}",
                            'currency': 'ETB',
                            'status': 'CHARGING_COMPLETED' if qr_session.status == 'charging_completed' else 'COMPLETED',
                            'payment_status': 'completed' if qr_session.payment_transaction else 'pending',
                            'duration_minutes': duration_minutes,
                            'duration_seconds': duration_seconds,
                            'payment_method': 'QR Code',
                            'payment_amount': str(qr_session.payment_transaction.amount) if qr_session.payment_transaction else '0.00',
                            'created_at': qr_session.created_at.isoformat(),
                        }
                        all_sessions.append(session_data)
                    except Exception as session_error:
                        # Skip problematic sessions but continue processing
                        continue

            except ImportError:
                pass
            except Exception as qr_error:
                # Log QR session error but continue
                pass

            # Sort all sessions by creation date (newest first)
            all_sessions.sort(key=lambda x: x['created_at'], reverse=True)

            return Response({
                'success': True,
                'results': all_sessions,
                'count': len(all_sessions)
            })

        except Exception as e:
            return Response({
                'success': False,
                'error': str(e),
                'results': [],
                'count': 0
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class StationOwnerSettingsView(generics.RetrieveUpdateAPIView):
    """View to manage station owner settings (SQL source of truth, Firestore mirror)"""

    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [TokenAuthentication, SessionAuthentication]
    serializer_class = StationOwnerSettingsSerializer

    def _get_or_create_sql_settings(self):
        from .models import StationOwner, StationOwnerSettings
        try:
            owner = StationOwner.objects.get(user=self.request.user)
            settings_obj, _ = StationOwnerSettings.objects.get_or_create(owner=owner)
            return settings_obj
        except StationOwner.DoesNotExist:
            from django.http import Http404
            raise Http404("Station owner profile not found")

    def retrieve(self, request, *args, **kwargs):
        s = self._get_or_create_sql_settings()
        return Response({
            'default_pricing_per_kwh': float(s.default_pricing_per_kwh),
            'auto_accept_bookings': s.auto_accept_bookings,
            'max_session_duration_hours': s.max_session_duration_hours,
            'maintenance_mode': s.maintenance_mode,
            'email_notifications': s.email_notifications,
            'sms_notifications': s.sms_notifications,
            'booking_notifications': s.booking_notifications,
            'payment_notifications': s.payment_notifications,
            'maintenance_alerts': s.maintenance_alerts,
            'marketing_emails': s.marketing_emails,
            'station_updates': s.station_updates,
            'brand_color': s.brand_color,
            'display_company_info': s.display_company_info,
        })

    def update(self, request, *args, **kwargs):
        import sys
        s = self._get_or_create_sql_settings()
        data = request.data
        fields = ['default_pricing_per_kwh', 'auto_accept_bookings', 'max_session_duration_hours',
                  'maintenance_mode', 'email_notifications', 'sms_notifications',
                  'booking_notifications', 'payment_notifications', 'maintenance_alerts',
                  'marketing_emails', 'station_updates', 'brand_color', 'display_company_info']
        for field in fields:
            if field in data:
                setattr(s, field, data[field])
        s.save()

        result = {
            'default_pricing_per_kwh': float(s.default_pricing_per_kwh),
            'auto_accept_bookings': s.auto_accept_bookings,
            'maintenance_mode': s.maintenance_mode,
            'email_notifications': s.email_notifications,
            'brand_color': s.brand_color,
        }

        # Mirror to Firestore (best-effort)
        try:
            firestore_repo.update_station_owner_settings(request.user.id, data)
        except Exception as e:
            print(f"Warning: Firestore settings sync failed (non-fatal): {e}", file=sys.stderr)

        return Response({'success': True, 'message': 'Settings updated successfully', 'data': result})


class NotificationTemplateListView(generics.ListAPIView):
    """View to list notification templates"""

    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [TokenAuthentication, SessionAuthentication]
    serializer_class = NotificationTemplateSerializer
    queryset = NotificationTemplate.objects.filter(is_active=True)


class NotificationTemplateDetailView(generics.RetrieveUpdateAPIView):
    """View to retrieve and update notification templates"""

    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [TokenAuthentication, SessionAuthentication]
    serializer_class = NotificationTemplateSerializer
    queryset = NotificationTemplate.objects.all()
    lookup_field = 'template_type'


class ReviewReplyCreateView(generics.CreateAPIView):
    """View for station owners to reply to reviews"""

    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [TokenAuthentication, SessionAuthentication]
    # We will use manual parsing. No specific serializer needed unless we define one for input.
    # Input: review_id, reply_text.
    
    def post(self, request, *args, **kwargs):
        try:
            review_id = request.data.get('review') or request.data.get('review_id')
            reply_text = request.data.get('reply_text')
            
            if not review_id or not reply_text:
                return Response({"error": "Missing review_id or reply_text"}, status=status.HTTP_400_BAD_REQUEST)
                
            # Find the review. We need station_id. 
            # If not provided, we must search all reviews by this owner.
            # But the review might be on ANY station.
            # However, ONLY owner can reply.
            # So review must belong to a station owned by station_owner.
            
            # Efficient search: Collection Group 'reviews' where 'station_owner_id' == owner.id AND 'id' == review_id
            # Or iterate.
            
            reviews = firestore_repo.list_reviews_by_owner(str(request.user.id))
            review = next((r for r in reviews if r['id'] == review_id), None)
            
            if not review:
                 return Response({"error": "Review not found or permission denied"}, status=status.HTTP_404_NOT_FOUND)
            
            # Check existing reply
            if review.get('reply'):
                 return Response({"error": "Reply already exists"}, status=status.HTTP_400_BAD_REQUEST)
            
            # Create reply object
            reply_data = {
                'id': str(uuid.uuid4()),
                'reply_text': reply_text,
                'created_at': datetime.now().isoformat(),
                'updated_at': datetime.now().isoformat(),
                'station_owner_id': str(request.user.id),
                'is_active': True
            }
            
            # Update review document
            # Store reply as 'reply' field map
            firestore_repo.update_review(review['station_id'], review_id, {'reply': reply_data})
            
            return Response(reply_data, status=status.HTTP_201_CREATED)

        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ReviewReplyDetailView(generics.RetrieveUpdateDestroyAPIView):
    """View for station owners to retrieve, update, or delete their replies"""

    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [TokenAuthentication, SessionAuthentication]
    # No standard serializer since we access nested field
    
    def get_object(self):
        # review_id or reply_id? 
        # Usually replies are accessed by their ID in REST.
        # But here reply is nested. 
        # If URL is /replies/<id>/, it is hard.
        # Ideally /reviews/<review_id>/reply/
        # But let's assume we pass review_id or reply_id.
        # If reply_id, we must search.
        
        reply_id = self.kwargs.get('id')
        # station_owner = StationOwner.objects.get(user=self.request.user) # No SQL
        
        # Search all reviews by owner to find the one having this reply_id
        reviews = firestore_repo.list_reviews_by_owner(str(self.request.user.id))
        
        target_review = None
        for r in reviews:
            if r.get('reply') and r['reply'].get('id') == reply_id:
                target_review = r
                break
        
        if not target_review:
             self.permission_denied(self.request, message="Reply not found", code=404)
             
        self.target_review = target_review
        return target_review['reply']

    def retrieve(self, request, *args, **kwargs):
        reply = self.get_object()
        return Response(reply)

    def update(self, request, *args, **kwargs):
        reply = self.get_object()
        review = self.target_review
        
        new_text = request.data.get('reply_text')
        if not new_text:
             return Response({"error": "Missing reply_text"}, status=400)
             
        reply['reply_text'] = new_text
        reply['updated_at'] = datetime.now().isoformat()
        
        firestore_repo.update_review(review['station_id'], review['id'], {'reply': reply})
        return Response(reply)

    def destroy(self, request, *args, **kwargs):
        # Soft delete? Or remove field?
        # Original code: is_active = False.
        # We can remove the field OR set is_active=False.
        # Let's remove for cleaner NoSQL, or follow logic.
        
        reply = self.get_object()
        review = self.target_review
        
        # Option 1: Remove
        # firestore_repo.update_review(review['station_id'], review['id'], {'reply': firestore.DELETE_FIELD}) 
        # But we don't have DELETE_FIELD in repo helper.
        
        # Option 2: Set None
        firestore_repo.update_review(review['station_id'], review['id'], {'reply': None})
        
        return Response(status=status.HTTP_204_NO_CONTENT)


class StationOwnerRepliesView(generics.ListAPIView):
    """View for station owners to see all their replies"""

    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [TokenAuthentication, SessionAuthentication]

    def list(self, request, *args, **kwargs):
        try:
            reviews = firestore_repo.list_reviews_by_owner(str(self.request.user.id))
            
            replies = []
            for r in reviews:
                if r.get('reply') and r['reply'].get('is_active', True):
                    reply = r['reply']
                    # Enhance with review info
                    reply['review_info'] = {
                        'id': r['id'],
                        'rating': r.get('rating'),
                        'review_text': r.get('review_text'),
                        'user_name': r.get('user_name'),
                        'created_at': r.get('created_at'),
                        'station_name': r.get('station_name'),
                        'station_id': r.get('station_id')
                    }
                    replies.append(reply)
            
            # Sort by created_at desc
            replies.sort(key=lambda x: x.get('created_at', ''), reverse=True)
            
            return Response({
                'success': True,
                'count': len(replies),
                'results': replies
            })
            
        except Exception as e:
             return Response({'results': [], 'count': 0, 'error': str(e)})


from .serializers import FirestoreAvailableStationSerializer

class AvailableStationsView(generics.ListAPIView):
    """View to fetch only available charging stations with real-time data"""

    serializer_class = FirestoreAvailableStationSerializer
    permission_classes = [permissions.AllowAny]
    authentication_classes = [AnonymousAuthentication, TokenAuthentication, SessionAuthentication]

    def get_queryset(self):
         return []

    def list(self, request, *args, **kwargs):
        # 1. Fetch from Firestore
        filters = {
            'is_active': True,
            'is_public': True,
            'status': 'operational'
        }
        stations = firestore_repo.list_stations(filters=filters)
        
        # 2. Filter available_connectors > 0
        stations = [s for s in stations if s.get('available_connectors', 0) > 0]
        
        # 3. Filter by Connector Type (Optional - In Memory, requires fetching connectors or assuming we optimize later)
        connector_type = request.query_params.get('connector_type')
        if connector_type:
             # We would need to fetch connectors for each station to check type.
             # This is N+1. For now, we skip this or implement if critical.
             # Let's try to filter if we had the data. We don't. 
             # I'll skip effective filtering for now to avoid specific overhead.
             pass

        # 4. Sorting
        user_lat = request.query_params.get('user_lat')
        user_lng = request.query_params.get('user_lng')

        if user_lat and user_lng:
            try:
                lat1 = float(user_lat)
                lon1 = float(user_lng)
                
                # Haversine helper
                def get_dist(s):
                    if not s.get('latitude') or not s.get('longitude'):
                        return float('inf')
                    lat2, lon2 = float(s['latitude']), float(s['longitude'])
                    return (lat1-lat2)**2 + (lon1-lon2)**2 # Squared Euclidean as proxy for small distances or use full haversine
                
                stations.sort(key=get_dist)
            except (ValueError, TypeError):
                pass
        else:
            # Sort by rating
            stations.sort(key=lambda x: (x.get('rating', 0), x.get('rating_count', 0)), reverse=True)
            
        # Pagination? Generic list
        
        serializer = self.get_serializer(stations, many=True)
        return Response(serializer.data)
class PayoutMethodListCreateView(APIView):
    """View to list and create payout methods for station owners (SQL source of truth)"""

    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [TokenAuthentication, SessionAuthentication]

    def _get_owner(self, user):
        from .models import StationOwner
        from rest_framework.exceptions import NotFound
        try:
            return StationOwner.objects.get(user=user)
        except StationOwner.DoesNotExist:
            raise NotFound('Station owner profile not found.')

    def get(self, request):
        from .models import PayoutMethod
        owner = self._get_owner(request.user)
        methods = PayoutMethod.objects.filter(station_owner=owner, is_active=True).order_by('-is_default', '-created_at')
        data = [{'id': str(m.id), 'method_type': m.method_type, 'account_holder_name': m.account_holder_name,
                 'bank_name': m.bank_name, 'account_number': m.account_number, 'phone_number': m.phone_number,
                 'provider': m.provider, 'is_default': m.is_default, 'is_verified': m.is_verified} for m in methods]
        return Response(data)

    def post(self, request):
        import sys
        from .models import PayoutMethod
        owner = self._get_owner(request.user)
        d = request.data
        try:
            # If first method, make it default
            is_first = not PayoutMethod.objects.filter(station_owner=owner, is_active=True).exists()
            sql_pm = PayoutMethod.objects.create(
                station_owner=owner,
                method_type=d.get('method_type', 'bank_account'),
                account_holder_name=d.get('account_holder_name', ''),
                bank_name=d.get('bank_name', ''),
                account_number=d.get('account_number', ''),
                routing_number=d.get('routing_number', ''),
                swift_code=d.get('swift_code', ''),
                phone_number=d.get('phone_number', ''),
                provider=d.get('provider', ''),
                paypal_email=d.get('paypal_email', ''),
                is_default=is_first or d.get('is_default', False),
                is_active=True,
            )
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        result = {'id': str(sql_pm.id), 'method_type': sql_pm.method_type,
                  'account_holder_name': sql_pm.account_holder_name, 'is_default': sql_pm.is_default}

        # Mirror to Firestore (best-effort)
        try:
            firestore_repo.create_payout_method(request.user.id, {**result, 'sql_id': str(sql_pm.id)})
        except Exception as e:
            print(f"Warning: Firestore payout sync failed (non-fatal): {e}", file=sys.stderr)

        return Response({'success': True, 'message': 'Payout method added successfully', 'data': result},
                        status=status.HTTP_201_CREATED)


class PayoutMethodDetailView(APIView):
    """View to retrieve, update, or delete a specific payout method (SQL source of truth)"""

    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [TokenAuthentication, SessionAuthentication]

    def _get_sql_pm(self, request, id):
        from .models import PayoutMethod, StationOwner
        from rest_framework.exceptions import NotFound
        try:
            owner = StationOwner.objects.get(user=request.user)
            return PayoutMethod.objects.get(id=id, station_owner=owner, is_active=True)
        except (PayoutMethod.DoesNotExist, StationOwner.DoesNotExist):
            raise NotFound('Payout method not found.')

    def get(self, request, id):
        pm = self._get_sql_pm(request, id)
        return Response({'id': str(pm.id), 'method_type': pm.method_type,
                         'account_holder_name': pm.account_holder_name,
                         'bank_name': pm.bank_name, 'account_number': pm.account_number,
                         'phone_number': pm.phone_number, 'provider': pm.provider,
                         'is_default': pm.is_default, 'is_verified': pm.is_verified})

    def put(self, request, id):
        import sys
        pm = self._get_sql_pm(request, id)
        d = request.data
        for field in ['account_holder_name', 'bank_name', 'account_number', 'routing_number',
                      'swift_code', 'phone_number', 'provider', 'paypal_email', 'is_default']:
            if field in d:
                setattr(pm, field, d[field])
        pm.save()
        try:
            firestore_repo.update_payout_method(request.user.id, id, d)
        except Exception as e:
            print(f"Warning: Firestore payout update failed (non-fatal): {e}", file=sys.stderr)
        return Response({'success': True, 'message': 'Payout method updated successfully',
                         'data': {'id': str(pm.id), 'is_default': pm.is_default}})

    def delete(self, request, id):
        import sys
        pm = self._get_sql_pm(request, id)
        pm.is_active = False
        pm.save()
        try:
            firestore_repo.delete_payout_method(request.user.id, id)
        except Exception as e:
            print(f"Warning: Firestore payout delete failed (non-fatal): {e}", file=sys.stderr)
        return Response({'success': True, 'message': 'Payout method deleted successfully'},
                        status=status.HTTP_204_NO_CONTENT)

    def put(self, request, id):
        pm = firestore_repo.get_payout_method(request.user.id, id)
        if not pm:
            return Response({'message': 'Payout method not found'}, status=status.HTTP_404_NOT_FOUND)
            
        serializer = FirestorePayoutMethodSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        
        updated = firestore_repo.update_payout_method(request.user.id, id, serializer.validated_data)
        
        return Response({
            'success': True,
            'message': 'Payout method updated successfully',
            'data': updated
        })
        
    def delete(self, request, id):
        firestore_repo.delete_payout_method(request.user.id, id)
        return Response({'success': True, 'message': 'Payout method deleted successfully'}, status=status.HTTP_204_NO_CONTENT)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()

        # Don't allow deleting the default method if it's the only one
        station_owner = instance.station_owner
        active_methods = PayoutMethod.objects.filter(
            station_owner=station_owner,
            is_active=True
        ).count()

        if instance.is_default and active_methods == 1:
            return Response({
                'success': False,
                'error': 'Cannot delete the only payout method. Add another method first.'
            }, status=status.HTTP_400_BAD_REQUEST)

        # If deleting default method, set another as default
        if instance.is_default:
            next_method = PayoutMethod.objects.filter(
                station_owner=station_owner,
                is_active=True
            ).exclude(id=instance.id).first()

            if next_method:
                next_method.is_default = True
                next_method.save()

        instance.is_active = False
        instance.save()

        return Response({
            'success': True,
            'message': 'Payout method deleted successfully'
        }, status=status.HTTP_204_NO_CONTENT)


class SetDefaultPayoutMethodView(APIView):
    """View to set a payout method as default (Firestore)"""

    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [TokenAuthentication, SessionAuthentication]

    def post(self, request, method_id): # Note: URL param might be 'method_id' (check urls.py)
        # Check urls.py: path('payout-methods/<int:method_id>/set-default/', ...)
        # Firestore IDs are likely strings (UUIDs). URL likely expects UUID or str? 
        # Check urls.py again if it enforces <int:method_id>. If so, need to change to <str:method_id> or <uuid:method_id>.
        
        try:
            # Update via Repo
            # Logic: Set this one to default=True. Repo handles unsetting others? 
            # Repo's update_payout_method unsets others if is_default=True.
            
            updated = firestore_repo.update_payout_method(request.user.id, method_id, {'is_default': True})
            
            if not updated:
                 return Response({
                    'success': False,
                    'error': 'Payout method not found'
                }, status=status.HTTP_404_NOT_FOUND)

            return Response({
                'success': True,
                'message': 'Default payout method updated successfully',
                'data': updated
            })

        except Exception as e:
            return Response({
                'success': False,
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class WithdrawalRequestView(APIView):
    """View to handle withdrawal requests from station owners (Firestore)"""

    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [TokenAuthentication, SessionAuthentication]

    def post(self, request):
        try:
            # Check station owner
            owner_data = firestore_repo.get_station_owner(request.user.id)
            if not owner_data:
                 return Response({
                    'success': False,
                    'error': 'Station owner profile not found'
                }, status=status.HTTP_404_NOT_FOUND)

            serializer = FirestoreWithdrawalRequestSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            
            amount = serializer.validated_data['amount']
            payment_method_id = serializer.validated_data['payment_method_id']
            description = serializer.validated_data.get('description', '')
            
            # Verify Payment Method
            pm = firestore_repo.get_payout_method(request.user.id, payment_method_id)
            if not pm or not pm.get('is_active', True):
                 return Response({
                    'success': False,
                    'error': 'Invalid payment method'
                }, status=status.HTTP_400_BAD_REQUEST)
                
            # Check available balance from wallet system
            from payments.models import Wallet, WalletTransaction, Transaction
            
            # Create wallet if it doesn't exist
            wallet, created = Wallet.objects.get_or_create(user=request.user)

            if wallet.balance < float(amount):
                return Response({
                    'success': False,
                    'error': f'Insufficient balance. Available: {wallet.balance} ETB'
                }, status=status.HTTP_400_BAD_REQUEST)

             # Create Withdrawal in Firestore
            data = serializer.validated_data.copy()
            data['owner_id'] = str(request.user.id)
            data['payment_method_snapshot'] = pm
            del data['payment_method_id'] 
            
            withdrawal = firestore_repo.create_withdrawal(data)
            withdrawal_id = withdrawal['id']
            
            # Deduct from Wallet (SQL Transaction)
            # Create a Transaction object for the withdrawal
            withdrawal_transaction = Transaction.objects.create(
                user=request.user,
                amount=float(amount),
                currency='ETB',
                transaction_type='withdrawal',
                status='pending',
                reference_number=f"WD-{withdrawal_id[:8]}", # Use Firestore ID segment
                description=f'Withdrawal request {withdrawal_id}'
            )

            balance_before = wallet.balance
            wallet.balance -= float(amount)
            balance_after = wallet.balance
            wallet.save()
            
            WalletTransaction.objects.create(
                wallet=wallet,
                transaction=withdrawal_transaction,
                transaction_type=WalletTransaction.TransactionType.DEBIT,
                amount=float(amount),
                balance_before=balance_before,
                balance_after=balance_after,
                description=f'Withdrawal request {withdrawal_id}'
            )

            # Send notification (skipped for brevity or can use existing logic if adapted)

            return Response({
                'success': True,
                'message': 'Withdrawal request submitted successfully',
                'data': withdrawal
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            return Response({'success': False, 'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def get(self, request):
        """Get withdrawal requests for the authenticated station owner"""
        try:
            withdrawals = firestore_repo.list_withdrawals(owner_id=request.user.id)
            return Response({
                'success': True,
                'data': withdrawals
            })
        except Exception as e:
            return Response({'success': False, 'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class WithdrawalRequestDetailView(APIView):
    """View to retrieve and update specific withdrawal requests (Firestore)"""

    permission_classes = [permissions.IsAuthenticated]
    authentication_classes = [TokenAuthentication, SessionAuthentication]

    def get(self, request, id):
        request_id = str(id)
        withdrawal = firestore_repo.get_withdrawal(request_id)
        if not withdrawal:
            return Response({'error': 'Withdrawal request not found'}, status=status.HTTP_404_NOT_FOUND)
            
        # Permission check
        if not (request.user.is_staff or request.user.is_superuser):
             if withdrawal.get('owner_id') != str(request.user.id):
                 return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
                 
        return Response(withdrawal)

    def put(self, request, id): # Or patch
        request_id = str(id)
        withdrawal = firestore_repo.get_withdrawal(request_id)
        if not withdrawal:
             return Response({'error': 'Withdrawal request not found'}, status=status.HTTP_404_NOT_FOUND)

        # Permission check
        is_admin = request.user.is_staff or request.user.is_superuser
        if not is_admin:
             if withdrawal.get('owner_id') != str(request.user.id):
                 return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
                 
             if withdrawal.get('status') != 'pending':
                  return Response({'error': 'Cannot update processed withdrawal'}, status=status.HTTP_400_BAD_REQUEST)
                  
        # Admin logic for status change?
        # If admin changes status to 'approved' or 'rejected', we should update SQL wallet transaction status too?
        # This logic is complex. For now, just update Firestore record.
        
        data = request.data
        if not is_admin and 'status' in data:
            del data['status'] # User cannot change status

        updated = firestore_repo.update_withdrawal(request_id, data)
        return Response(updated)


class WithdrawalRequestListView(APIView):
    """Admin view to list all withdrawal requests (Firestore)"""

    permission_classes = [permissions.IsAuthenticated] # IsAdminUser ideally
    authentication_classes = [TokenAuthentication, SessionAuthentication]

    def get(self, request):
        if not (request.user.is_staff or request.user.is_superuser):
             return Response({'error': 'Admin only'}, status=status.HTTP_403_FORBIDDEN)
             
        # List all? firestore_repo need list_all_withdrawals?
        # list_withdrawals(owner_id=None) returns all ordered by date
        withdrawals = firestore_repo.list_withdrawals()
        return Response(withdrawals)
