from django.db.models.signals import post_save
from django.dispatch import receiver
import logging

logger = logging.getLogger(__name__)


@receiver(post_save, sender='charging_stations.StationOwner')
def sync_station_owner_to_firestore(sender, instance, **kwargs):
    """
    When an admin saves a StationOwner in Django Admin (SQL),
    sync the verification_status and key fields back to Firestore
    so the frontend always reflects the latest status.
    """
    try:
        from utils.firestore_repo import firestore_repo
        firestore_repo.update_station_owner(instance.user_id, {
            'verification_status': instance.verification_status,
            'is_profile_completed': instance.is_profile_completed,
            'company_name': instance.company_name,
            'contact_email': instance.contact_email or '',
            'contact_phone': instance.contact_phone or '',
            'business_registration_number': instance.business_registration_number or '',
            'website': instance.website or '',
            'description': instance.description or '',
        })
        logger.info(f"Synced StationOwner {instance.user_id} to Firestore: status={instance.verification_status}")
    except Exception as e:
        logger.error(f"Failed to sync StationOwner {instance.user_id} to Firestore: {e}")
